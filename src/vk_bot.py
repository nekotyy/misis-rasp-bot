from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from aiohttp import TCPConnector
from vkbottle import API
from vkbottle import Keyboard, Text
from vkbottle.bot import Bot, Message
from vkbottle.http import AiohttpClient

from src.config import Settings
from src.db import Database
from src.homework_service import SUBJECTS, get_subject
from src.models import HomeworkAttachment, HomeworkDraft
from src.parser import ScheduleParser
from src.schedule_service import get_day_by_offset, get_day_by_offset_from_content


def build_vk_bot(settings: Settings, db: Database, parser: ScheduleParser) -> Bot | None:
    if not settings.vk_bot_token:
        return None

    api = None
    if settings.vk_disable_ssl_verify:
        api = API(
            settings.vk_bot_token,
            http_client=AiohttpClient(connector=TCPConnector(ssl=False)),
        )

    bot = Bot(token=settings.vk_bot_token, api=api)
    homework_drafts: dict[int, HomeworkDraft] = {}
    context_messages: dict[int, dict[str, list[int]]] = defaultdict(dict)
    editor_option_map: dict[int, dict[str, int]] = defaultdict(dict)
    homework_delete_option_map: dict[int, dict[str, tuple[int, str]]] = defaultdict(dict)
    peer_modes: dict[int, str] = {}

    def text_keyboard(*rows: list[str], one_time: bool = False) -> str:
        keyboard = Keyboard(one_time=one_time, inline=False)
        for row_index, row in enumerate(rows):
            if row_index:
                keyboard.row()
            for label in row:
                keyboard.add(Text(label))
        return keyboard.get_json()

    def welcome_text(is_editor: bool = False) -> str:
        editor_hint = "\n/dz - добавить домашнее задание" if is_editor else ""
        return (
            f"Привет! Я бот группы {settings.group_name}\n\n"
            "/rasp - посмотреть расписание\n"
            "/homework - посмотреть домашние задания"
            f"{editor_hint}"
        )

    def format_day_text(day, fallback_label: str) -> str:
        if day is None:
            return f"Расписание на {fallback_label}\n\nПар нет."
        if not day.lessons:
            return f"Расписание на {day.date_label}\n\nПар нет."

        lines = [f"Расписание на {day.date_label}", ""]
        for lesson in day.lessons:
            lines.append(f"{lesson.number}. в {lesson.classroom} по {lesson.subject} у {lesson.teacher}")
        return "\n".join(lines)

    def format_vk_homework(entry: dict, success_title: str | None = None) -> str:
        created_at = datetime.fromisoformat(entry["created_at"])
        lines = []
        if success_title:
            lines.extend([success_title, ""])
        lines.extend(
            [
                f"{entry['subject']} - {entry['teacher']} | #{entry['id']} | {entry['created_by_name']}",
                "-------",
                entry["text"],
                "-------",
                created_at.strftime("%d.%m.%Y %H:%M"),
            ]
        )
        return "\n".join(lines)

    def format_vk_preview(draft: HomeworkDraft, author: str) -> str:
        return "\n".join(
            [
                f"{draft.subject_name} - {draft.teacher_name} | предпросмотр | {author}",
                "-------",
                draft.text,
                "-------",
                "Будет сохранено после подтверждения",
            ]
        )

    def schedule_keyboard() -> str:
        return text_keyboard(
            ["Расписание на сегодня"],
            ["Расписание на завтра"],
            ["Расписание на 2 дня"],
            ["Назад"],
        )

    def homework_subject_keyboard() -> str:
        return text_keyboard(*[[subject["subject"]] for subject in SUBJECTS], ["Назад"])

    def homework_view_keyboard() -> str:
        return text_keyboard(["Вернуться к списку ДЗ"])

    def draft_preview_keyboard() -> str:
        return text_keyboard(["Добавить вложения"], ["Опубликовать"], ["Отменить"])

    def draft_attachment_keyboard() -> str:
        return text_keyboard(["Опубликовать"], ["Отменить"])

    def admin_keyboard() -> str:
        return text_keyboard(
            ["Статус", "Перепарсить"],
            ["Сохранить эталон", "Последнее изменение"],
            ["Пользователи", "Редакторы"],
            ["Удалить ДЗ", "Тестовая рассылка"],
            ["Закрыть админку"],
        )

    def admin_users_keyboard() -> str:
        return text_keyboard(["Назад в админку"])

    def admin_subjects_keyboard() -> str:
        return text_keyboard(*[[subject["subject"]] for subject in SUBJECTS], ["Назад в админку"])

    def build_editor_keyboard(peer_id: int, users: list) -> str:
        labels: dict[str, int] = {}
        rows: list[list[str]] = []
        for user in users:
            if user.platform != "vk":
                continue
            display = user.full_name or user.username or str(user.user_id)
            prefix = "Убрать редактора" if user.is_editor else "Сделать редактором"
            label = f"{prefix}: {display} ({user.user_id})"
            labels[label] = user.user_id
            rows.append([label])
        rows.append(["Назад в админку"])
        editor_option_map[peer_id] = labels
        return text_keyboard(*rows)

    def build_delete_keyboard(peer_id: int, subject_key: str, entries: list[dict]) -> str:
        labels: dict[str, tuple[int, str]] = {}
        rows: list[list[str]] = []
        for entry in entries:
            preview = entry["text"].strip().replace("\n", " ")
            if len(preview) > 24:
                preview = f"{preview[:24].rstrip()}..."
            label = f"Удалить #{entry['id']} {preview or 'без текста'}"
            labels[label] = (entry["id"], subject_key)
            rows.append([label])
        rows.append(["Назад к предметам"])
        rows.append(["Назад в админку"])
        homework_delete_option_map[peer_id] = labels
        return text_keyboard(*rows)

    async def register_user(message: Message) -> None:
        if message.from_id is None:
            return
        is_admin = bool(settings.admin_vk_id and message.from_id == settings.admin_vk_id)
        existing = await db.get_user("vk", message.from_id)
        await db.upsert_user(
            platform="vk",
            user_id=message.from_id,
            username=None,
            full_name=None,
            is_admin=is_admin,
            is_editor=existing.is_editor if existing else False,
        )

    def is_admin(user_id: int | None) -> bool:
        return bool(user_id and settings.admin_vk_id and user_id == settings.admin_vk_id)

    async def is_editor(user_id: int | None) -> bool:
        if user_id is None:
            return False
        user = await db.get_user("vk", user_id)
        return bool(user and user.is_editor)

    async def delete_cmids(peer_id: int, cmids: list[int]) -> None:
        if not cmids:
            return
        try:
            await bot.api.messages.delete(peer_id=peer_id, cmids=cmids, delete_for_all=True)
        except Exception:
            pass

    async def clear_context(peer_id: int, context: str) -> None:
        await delete_cmids(peer_id, context_messages[peer_id].get(context, []))
        context_messages[peer_id][context] = []

    async def replace_context(
        peer_id: int,
        context: str,
        text: str,
        keyboard: str | None = None,
        attachment: str | None = None,
    ) -> None:
        cmids = context_messages[peer_id].get(context, [])
        if cmids:
            try:
                await bot.api.messages.edit(
                    peer_id=peer_id,
                    cmid=cmids[0],
                    message=text,
                    keyboard=keyboard,
                    attachment=attachment,
                )
                if len(cmids) > 1:
                    await delete_cmids(peer_id, cmids[1:])
                context_messages[peer_id][context] = [cmids[0]]
                return
            except Exception:
                await clear_context(peer_id, context)

        response = await bot.api.messages.send(
            peer_ids=[peer_id],
            message=text,
            keyboard=keyboard,
            attachment=attachment,
            random_id=0,
        )
        context_messages[peer_id][context] = [response[0].conversation_message_id]

    async def delete_incoming_message(message: Message) -> None:
        if message.peer_id is None or message.conversation_message_id is None:
            return
        await delete_cmids(message.peer_id, [message.conversation_message_id])

    def subject_by_title(title: str) -> dict[str, str] | None:
        normalized = title.strip().casefold()
        for subject in SUBJECTS:
            if subject["subject"].casefold() == normalized:
                return subject
        return None

    async def latest_snapshot(snapshot_type: str) -> dict | None:
        return await db.get_latest_snapshot(snapshot_type)

    def snapshot_line(label: str, snapshot_row: dict | None) -> str:
        if snapshot_row is None:
            return f"{label}: еще не было"
        return (
            f"{label}: {snapshot_row['created_at']}\n"
            f"Сайт отдал данные: {snapshot_row['fetched_at']}"
        )

    async def admin_status_text() -> str:
        users = await db.list_users()
        homework_count = await db.count_homework_entries()
        current_snapshot = await db.get_latest_snapshot("current")
        baseline_snapshot = await db.get_latest_snapshot("daily_baseline")
        last_change = await db.get_last_change()
        editor_count = sum(1 for user in users if user.is_editor)
        last_change_at = last_change["created_at"] if last_change else "еще не было"
        return (
            "Статус бота\n\n"
            f"Группа: {settings.group_name}\n"
            f"Пользователей: {len(users)}\n"
            f"Редакторов: {editor_count}\n"
            f"Записей ДЗ: {homework_count}\n"
            f"Последнее изменение: {last_change_at}\n\n"
            f"{snapshot_line('Последний обычный парс', current_snapshot)}\n\n"
            f"{snapshot_line('Последний сохраненный эталон', baseline_snapshot)}"
        )

    async def send_schedule_menu(peer_id: int) -> None:
        peer_modes[peer_id] = "schedule_menu"
        await replace_context(peer_id, "menu", "Выбери нужный вариант расписания.", keyboard=schedule_keyboard())

    async def send_homework_subjects(peer_id: int) -> None:
        peer_modes[peer_id] = "homework_subjects"
        await replace_context(peer_id, "menu", "Выбери предмет. После выбора я покажу последнее домашнее задание.", keyboard=homework_subject_keyboard())

    async def send_dz_subjects(peer_id: int) -> None:
        peer_modes[peer_id] = "dz_subjects"
        await replace_context(peer_id, "dz", "Выбери предмет для нового домашнего задания.", keyboard=homework_subject_keyboard())

    async def send_latest_homework(peer_id: int, subject_key: str) -> None:
        subject = get_subject(subject_key)
        if subject is None:
            await replace_context(peer_id, "homework", "Предмет не найден.", keyboard=homework_view_keyboard())
            return
        entries = await db.get_homework_for_subject(subject_key)
        if not entries:
            await replace_context(
                peer_id,
                "homework",
                f"По предмету {subject['subject']} пока нет домашних заданий.",
                keyboard=homework_view_keyboard(),
            )
            peer_modes[peer_id] = "homework_entry"
            return

        entry = entries[0]
        attachment_refs = [attachment["file_id"] for attachment in entry["attachments"] if attachment["file_type"] == "vk_attachment"]
        await clear_context(peer_id, "menu")
        peer_modes[peer_id] = "homework_entry"
        await replace_context(
            peer_id,
            "homework",
            format_vk_homework(entry),
            keyboard=homework_view_keyboard(),
            attachment=",".join(attachment_refs) if attachment_refs else None,
        )

    async def send_draft_preview(peer_id: int, author: str, draft: HomeworkDraft) -> None:
        attachment_refs = [attachment.file_id for attachment in (draft.attachments or []) if attachment.file_type == "vk_attachment"]
        draft.awaiting_attachments = False
        peer_modes[peer_id] = "dz_preview"
        await replace_context(
            peer_id,
            "dz",
            format_vk_preview(draft, author),
            keyboard=draft_preview_keyboard(),
            attachment=",".join(attachment_refs) if attachment_refs else None,
        )

    async def publish_homework(peer_id: int, user_id: int, author: str) -> None:
        draft = homework_drafts[user_id]
        homework_id = await db.create_homework(
            subject_key=draft.subject_key,
            subject=draft.subject_name,
            teacher=draft.teacher_name,
            text=draft.text,
            created_by_platform="vk",
            created_by_user_id=user_id,
            created_by_name=author,
            attachments=draft.attachments or [],
        )
        entry = {
            "id": homework_id,
            "subject_key": draft.subject_key,
            "subject": draft.subject_name,
            "teacher": draft.teacher_name,
            "text": draft.text,
            "created_by_name": author,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "attachments": [
                {
                    "file_id": attachment.file_id,
                    "file_type": attachment.file_type,
                    "file_name": attachment.file_name,
                    "mime_type": attachment.mime_type,
                }
                for attachment in (draft.attachments or [])
            ],
        }
        attachment_refs = [attachment["file_id"] for attachment in entry["attachments"] if attachment["file_type"] == "vk_attachment"]
        await replace_context(
            peer_id,
            "dz",
            format_vk_homework(entry, success_title="Домашнее задание успешно создано"),
            keyboard=None,
            attachment=",".join(attachment_refs) if attachment_refs else None,
        )
        homework_drafts.pop(user_id, None)
        peer_modes[peer_id] = "idle"

    @bot.on.message()
    async def all_messages_handler(message: Message) -> None:
        await register_user(message)
        if message.peer_id is None or message.from_id is None:
            return

        peer_id = message.peer_id
        user_id = message.from_id
        text = (message.text or "").strip()
        text_lower = text.casefold()
        current_mode = peer_modes.get(peer_id, "idle")
        draft = homework_drafts.get(user_id)

        if text_lower in {"/start", "start", "начать"}:
            homework_drafts.pop(user_id, None)
            peer_modes[peer_id] = "idle"
            await clear_context(peer_id, "menu")
            await clear_context(peer_id, "homework")
            await clear_context(peer_id, "dz")
            await clear_context(peer_id, "admin")
            await message.answer(welcome_text(is_editor=await is_editor(user_id)))
            return

        if text_lower == "/rasp":
            await send_schedule_menu(peer_id)
            return

        if current_mode == "schedule_menu":
            snapshot_row = await latest_snapshot("current")
            if text == "Назад":
                peer_modes[peer_id] = "idle"
                await replace_context(peer_id, "menu", welcome_text(is_editor=await is_editor(user_id)))
                return
            if snapshot_row is None:
                await replace_context(peer_id, "menu", "Сохраненное расписание пока отсутствует.", keyboard=schedule_keyboard())
                return
            if text == "Расписание на сегодня":
                day = get_day_by_offset_from_content(snapshot_row["content"], 0)
                await replace_context(peer_id, "menu", format_day_text(day, "сегодня"), keyboard=schedule_keyboard())
                return
            if text == "Расписание на завтра":
                day = get_day_by_offset_from_content(snapshot_row["content"], 1)
                await replace_context(peer_id, "menu", format_day_text(day, "завтра"), keyboard=schedule_keyboard())
                return
            if text == "Расписание на 2 дня":
                day = get_day_by_offset_from_content(snapshot_row["content"], 2)
                await replace_context(peer_id, "menu", format_day_text(day, "2 дня"), keyboard=schedule_keyboard())
                return

        if text_lower == "/homework":
            await send_homework_subjects(peer_id)
            return

        if text == "Вернуться к списку ДЗ":
            await clear_context(peer_id, "homework")
            await send_homework_subjects(peer_id)
            return

        if current_mode == "homework_subjects":
            if text == "Назад":
                peer_modes[peer_id] = "idle"
                await replace_context(peer_id, "menu", welcome_text(is_editor=await is_editor(user_id)))
                return
            subject = subject_by_title(text)
            if subject is not None:
                await send_latest_homework(peer_id, subject["key"])
                return

        if text_lower == "/dz":
            if not await is_editor(user_id):
                await replace_context(peer_id, "dz", "Команда доступна только редакторам домашнего задания.")
                return
            await send_dz_subjects(peer_id)
            return

        if text == "Отменить":
            homework_drafts.pop(user_id, None)
            peer_modes[peer_id] = "idle"
            await clear_context(peer_id, "dz")
            await replace_context(peer_id, "menu", welcome_text(is_editor=await is_editor(user_id)))
            return

        if current_mode == "dz_subjects":
            if text == "Назад":
                peer_modes[peer_id] = "idle"
                await replace_context(peer_id, "menu", welcome_text(is_editor=await is_editor(user_id)))
                return
            subject = subject_by_title(text)
            if subject is not None:
                homework_drafts[user_id] = HomeworkDraft(
                    subject_key=subject["key"],
                    subject_name=subject["subject"],
                    teacher_name=subject["teacher"],
                )
                peer_modes[peer_id] = "dz_text"
                await replace_context(
                    peer_id,
                    "dz",
                    f"Выбран предмет {subject['subject']}.\n\nТеперь отправь текст домашнего задания одним сообщением.",
                    keyboard=text_keyboard(["Отменить"]),
                )
                return

        if draft is not None and draft.awaiting_text and text and text not in {"Добавить вложения", "Опубликовать"}:
            draft.text = text
            draft.awaiting_text = False
            draft.awaiting_attachments = False
            await delete_incoming_message(message)
            await send_draft_preview(peer_id, str(user_id), draft)
            return

        if text == "Добавить вложения":
            if draft is None or not draft.text.strip():
                await replace_context(peer_id, "dz", "Сначала добавь текст домашнего задания.")
                return
            draft.awaiting_attachments = True
            peer_modes[peer_id] = "dz_attachments"
            await replace_context(
                peer_id,
                "dz",
                "Отправь вложения сообщениями: документ, фото, видео или аудио.\n\nПосле каждого файла я обновлю предпросмотр. Когда закончишь, нажми «Опубликовать».",
                keyboard=draft_attachment_keyboard(),
            )
            return

        if draft is not None and draft.awaiting_attachments:
            full_message = await message.get_full_message()
            attachment_strings = full_message.get_attachment_strings() or []
            if attachment_strings:
                for attachment_ref in attachment_strings:
                    draft.attachments.append(
                        HomeworkAttachment(
                            file_id=attachment_ref,
                            file_type="vk_attachment",
                            file_name=None,
                            mime_type=None,
                        )
                    )
                await delete_incoming_message(message)
                await send_draft_preview(peer_id, str(user_id), draft)
                return
            if text == "Опубликовать" and draft.text.strip():
                await publish_homework(peer_id, user_id, str(user_id))
                return
            await replace_context(
                peer_id,
                "dz",
                "Сейчас я жду вложение. Отправь документ, фото, видео или аудио, либо нажми «Опубликовать».",
                keyboard=draft_attachment_keyboard(),
            )
            return

        if text == "Опубликовать":
            if draft is None or not draft.text.strip():
                await replace_context(peer_id, "dz", "Нет готового черновика для публикации.")
                return
            await publish_homework(peer_id, user_id, str(user_id))
            return

        if text_lower == "/admin":
            if not is_admin(user_id):
                await replace_context(peer_id, "admin", "Команда доступна только администратору.")
                return
            peer_modes[peer_id] = "admin"
            await replace_context(peer_id, "admin", "Админ-панель\n\nВыбери нужное действие.", keyboard=admin_keyboard())
            return

        if is_admin(user_id):
            if text == "Закрыть админку":
                peer_modes[peer_id] = "idle"
                await replace_context(peer_id, "admin", welcome_text(is_editor=await is_editor(user_id)))
                return
            if text == "Назад в админку":
                peer_modes[peer_id] = "admin"
                await replace_context(peer_id, "admin", "Админ-панель\n\nВыбери нужное действие.", keyboard=admin_keyboard())
                return
            if text == "Статус":
                peer_modes[peer_id] = "admin"
                await replace_context(peer_id, "admin", await admin_status_text(), keyboard=admin_keyboard())
                return
            if text == "Перепарсить":
                snapshot, snapshot_hash = await parser.parse()
                await db.save_snapshot("current", snapshot_hash, snapshot)
                day = get_day_by_offset(snapshot, 0)
                preview = format_day_text(day, "сегодня")
                await replace_context(peer_id, "admin", "Расписание перепарсено\n\n" + preview, keyboard=admin_keyboard())
                return
            if text == "Сохранить эталон":
                snapshot, snapshot_hash = await parser.parse()
                await db.save_snapshot("daily_baseline", snapshot_hash, snapshot)
                day = get_day_by_offset(snapshot, 0)
                preview = format_day_text(day, "сегодня")
                await replace_context(peer_id, "admin", "Эталон для сравнения сохранен\n\n" + preview, keyboard=admin_keyboard())
                return
            if text == "Последнее изменение":
                last_change = await db.get_last_change()
                response_text = (
                    "Последнее изменение\n\nИзменений пока не было."
                    if not last_change
                    else f"Последнее изменение\n\n{last_change['created_at']}\n\n{last_change['message']}"
                )
                await replace_context(peer_id, "admin", response_text, keyboard=admin_keyboard())
                return
            if text == "Пользователи":
                users = await db.list_users()
                if not users:
                    response_text = "Пользователи\n\nПока никто не зарегистрирован."
                else:
                    lines = ["Пользователи", ""]
                    for user in users:
                        display = user.full_name or user.username or "Без имени"
                        roles = []
                        if user.is_admin:
                            roles.append("админ")
                        if user.is_editor:
                            roles.append("редактор")
                        role_text = f" ({', '.join(roles)})" if roles else ""
                        lines.append(f"- {user.platform} | {display} | {user.user_id}{role_text}")
                    response_text = "\n".join(lines)
                await replace_context(peer_id, "admin", response_text, keyboard=admin_users_keyboard())
                return
            if text == "Редакторы":
                users = await db.list_users("vk")
                await replace_context(
                    peer_id,
                    "admin",
                    "Управление редакторами\n\nВыбери пользователя, чтобы выдать или снять роль редактора.",
                    keyboard=build_editor_keyboard(peer_id, users),
                )
                peer_modes[peer_id] = "admin_editors"
                return
            if text in editor_option_map.get(peer_id, {}):
                target_id = editor_option_map[peer_id][text]
                target = await db.get_user("vk", target_id)
                if target is not None:
                    await db.set_editor("vk", target_id, not target.is_editor)
                users = await db.list_users("vk")
                await replace_context(
                    peer_id,
                    "admin",
                    "Управление редакторами\n\nРоль обновлена. Выбери пользователя, чтобы продолжить.",
                    keyboard=build_editor_keyboard(peer_id, users),
                )
                peer_modes[peer_id] = "admin_editors"
                return
            if text == "Удалить ДЗ":
                peer_modes[peer_id] = "admin_delete_subject"
                await replace_context(
                    peer_id,
                    "admin",
                    "Удаление домашнего задания\n\nВыбери предмет, чтобы увидеть последние записи.",
                    keyboard=admin_subjects_keyboard(),
                )
                return
            if current_mode == "admin_delete_subject":
                if text == "Назад к предметам":
                    await replace_context(
                        peer_id,
                        "admin",
                        "Удаление домашнего задания\n\nВыбери предмет, чтобы увидеть последние записи.",
                        keyboard=admin_subjects_keyboard(),
                    )
                    return
                subject = subject_by_title(text)
                if subject is not None:
                    entries = await db.get_homework_for_subject(subject["key"])
                    if not entries:
                        await replace_context(
                            peer_id,
                            "admin",
                            f"{subject['subject']}\n\nДля этого предмета пока нет записей.",
                            keyboard=admin_subjects_keyboard(),
                        )
                    else:
                        await replace_context(
                            peer_id,
                            "admin",
                            f"{subject['subject']}\n\nВыбери запись, которую нужно удалить.",
                            keyboard=build_delete_keyboard(peer_id, subject["key"], entries),
                        )
                        peer_modes[peer_id] = "admin_delete_entry"
                    return
            if current_mode == "admin_delete_entry":
                if text == "Назад к предметам":
                    peer_modes[peer_id] = "admin_delete_subject"
                    await replace_context(
                        peer_id,
                        "admin",
                        "Удаление домашнего задания\n\nВыбери предмет, чтобы увидеть последние записи.",
                        keyboard=admin_subjects_keyboard(),
                    )
                    return
                if text in homework_delete_option_map.get(peer_id, {}):
                    homework_id, subject_key = homework_delete_option_map[peer_id][text]
                    deleted = await db.delete_homework(homework_id)
                    subject = get_subject(subject_key)
                    entries = await db.get_homework_for_subject(subject_key)
                    subject_name = subject["subject"] if subject else "Предмет"
                    if not entries:
                        peer_modes[peer_id] = "admin_delete_subject"
                        response_text = f"{subject_name}\n\n" + ("Запись удалена." if deleted else "Запись не найдена.")
                        keyboard = admin_subjects_keyboard()
                    else:
                        response_text = f"{subject_name}\n\n" + (
                            "Запись удалена.\n\nВыбери следующую запись для удаления."
                            if deleted
                            else "Запись не найдена.\n\nВыбери следующую запись для удаления."
                        )
                        keyboard = build_delete_keyboard(peer_id, subject_key, entries)
                    await replace_context(peer_id, "admin", response_text, keyboard=keyboard)
                    return
            if text == "Тестовая рассылка":
                users = await db.get_users_for_platform("vk")
                for user in users:
                    try:
                        await bot.api.messages.send(
                            peer_ids=[user.user_id],
                            message="Тестовое уведомление: бот активен и рассылка работает.",
                            random_id=0,
                        )
                    except Exception:
                        continue
                await replace_context(peer_id, "admin", "Тестовая рассылка\n\nСообщение отправлено всем зарегистрированным пользователям VK.", keyboard=admin_keyboard())
                return

        await replace_context(
            peer_id,
            "menu",
            "Используй /rasp для расписания, /homework для просмотра ДЗ, /dz для добавления домашки и /admin для админки, если у тебя есть права.",
        )

    return bot
