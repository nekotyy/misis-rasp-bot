from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from aiohttp import TCPConnector
from vkbottle import API, Keyboard, Text
from vkbottle.bot import Bot, Message
from vkbottle.http import AiohttpClient
from vkbottle.tools.uploader.doc import DocMessagesUploader
from vkbottle.tools.uploader.photo import PhotoMessageUploader

from src.attachment_storage import AttachmentStorage
from src.config import Settings
from src.db import Database
from src.homework_service import SUBJECTS, format_homework_notification, get_subject
from src.models import HomeworkAttachment, HomeworkDraft
from src.notifier import Broadcaster
from src.parser import ScheduleParser
from src.schedule_service import get_day_by_offset, get_day_by_offset_from_content

PAGE_SIZE = 6


def build_vk_bot(
    settings: Settings,
    db: Database,
    parser: ScheduleParser,
    broadcaster: Broadcaster | None = None,
    attachment_storage: AttachmentStorage | None = None,
) -> Bot | None:
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
    peer_modes: dict[int, str] = {}
    peer_pages: dict[int, dict[str, int]] = defaultdict(dict)
    editor_option_map: dict[int, dict[str, int]] = defaultdict(dict)
    delete_option_map: dict[int, dict[str, tuple[int, str]]] = defaultdict(dict)

    def make_keyboard(rows: list[list[str]]) -> str:
        keyboard = Keyboard(one_time=False, inline=False)
        for row_index, row in enumerate(rows):
            if row_index:
                keyboard.row()
            for label in row:
                keyboard.add(Text(label))
        return keyboard.get_json()

    def paged_rows(items: list[str], page: int) -> tuple[list[list[str]], int]:
        total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        chunk = items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        rows = [[item] for item in chunk]
        nav: list[str] = []
        if page > 0:
            nav.append("Предыдущая страница")
        if page < total_pages - 1:
            nav.append("Следующая страница")
        if nav:
            rows.append(nav)
        return rows, page

    def shorten_button_label(text: str, limit: int = 40) -> str:
        clean = " ".join(text.split())
        if len(clean) <= limit:
            return clean
        return f"{clean[: limit - 3].rstrip()}..."

    def user_is_admin(user_id: int | None) -> bool:
        return bool(user_id and settings.admin_vk_id and user_id == settings.admin_vk_id)

    async def user_is_editor(user_id: int | None) -> bool:
        if user_id is None:
            return False
        user = await db.get_user("vk", user_id)
        return bool(user and user.is_editor)

    async def fetch_vk_names(user_ids: list[int]) -> dict[int, str]:
        unique_ids = sorted({user_id for user_id in user_ids if user_id > 0})
        if not unique_ids:
            return {}
        profiles = await bot.api.users.get(user_ids=unique_ids)
        result: dict[int, str] = {}
        for profile in profiles:
            full_name = " ".join(part for part in [profile.first_name, profile.last_name] if part).strip()
            if full_name:
                result[profile.id] = full_name
        return result

    async def sync_vk_user_names(user_ids: list[int]) -> dict[int, str]:
        names = await fetch_vk_names(user_ids)
        for user_id, full_name in names.items():
            existing = await db.get_user("vk", user_id)
            if existing is None:
                continue
            await db.upsert_user(
                platform="vk",
                user_id=user_id,
                username=existing.username,
                full_name=full_name,
                is_admin=existing.is_admin,
                is_editor=existing.is_editor,
            )
        return names

    async def register_user(message: Message) -> None:
        if message.from_id is None:
            return
        names = await fetch_vk_names([message.from_id])
        existing = await db.get_user("vk", message.from_id)
        full_name = names.get(message.from_id) or (existing.full_name if existing else None)
        await db.upsert_user(
            platform="vk",
            user_id=message.from_id,
            username=None,
            full_name=full_name,
            is_admin=user_is_admin(message.from_id),
            is_editor=existing.is_editor if existing else False,
        )

    async def show_screen(peer_id: int, text: str, keyboard: str | None = None, attachment: str | None = None) -> None:
        await bot.api.messages.send(
            peer_ids=[peer_id],
            message=text,
            keyboard=keyboard,
            attachment=attachment,
            random_id=0,
        )

    async def upload_attachment_for_vk(peer_id: int, attachment: HomeworkAttachment | dict) -> str | None:
        file_type = attachment.file_type if isinstance(attachment, HomeworkAttachment) else attachment.get("file_type")
        file_id = attachment.file_id if isinstance(attachment, HomeworkAttachment) else attachment.get("file_id")
        storage_path = attachment.storage_path if isinstance(attachment, HomeworkAttachment) else attachment.get("storage_path")
        if file_id and not storage_path and file_type == "vk_attachment":
            return file_id
        if attachment_storage is None:
            return file_id if file_type == "vk_attachment" else None

        local_path = attachment_storage.resolve_path(storage_path)
        if not local_path or not local_path.exists():
            return file_id if file_type == "vk_attachment" else None

        if file_type == "photo":
            return await PhotoMessageUploader(bot.api).upload(str(local_path), peer_id=peer_id)

        file_name = attachment.file_name if isinstance(attachment, HomeworkAttachment) else attachment.get("file_name")
        return await DocMessagesUploader(bot.api).upload(
            str(local_path),
            peer_id=peer_id,
            title=file_name or local_path.name,
        )

    async def collect_vk_attachments(peer_id: int, attachments: list[HomeworkAttachment | dict]) -> str | None:
        uploaded: list[str] = []
        for attachment in attachments:
            uploaded_attachment = await upload_attachment_for_vk(peer_id, attachment)
            if uploaded_attachment:
                uploaded.append(uploaded_attachment)
        return ",".join(uploaded) if uploaded else None

    def menu_keyboard(is_editor: bool, is_admin: bool) -> str:
        rows = [["Расписание"], ["Домашние задания"], ["Настройки"]]
        if is_editor:
            rows.append(["Добавить ДЗ"])
        if is_admin:
            rows.append(["Админка"])
        return make_keyboard(rows)

    def schedule_keyboard() -> str:
        return make_keyboard(
            [
                ["Расписание на сегодня"],
                ["Расписание на завтра"],
                ["Расписание на 2 дня"],
                ["Назад в меню"],
            ]
        )

    def homework_view_keyboard() -> str:
        return make_keyboard([["Вернуться к списку ДЗ"], ["Назад в меню"]])

    def draft_preview_keyboard() -> str:
        return make_keyboard([["Добавить вложения"], ["Опубликовать"], ["Отменить"]])

    def draft_attachment_keyboard() -> str:
        return make_keyboard([["Опубликовать"], ["Отменить"]])

    def settings_keyboard(linked: bool, notifications_enabled: bool, awaiting_code: bool = False) -> str:
        rows = [
            [
                "Выключить уведомления о ДЗ"
                if notifications_enabled
                else "Включить уведомления о ДЗ"
            ]
        ]
        if linked:
            rows.append(["Отвязать Telegram"])
        else:
            rows.append(["У меня есть код привязки"])
        if awaiting_code:
            rows.append(["Отменить ввод кода"])
        rows.append(["Назад в меню"])
        return make_keyboard(rows)

    def admin_keyboard() -> str:
        return make_keyboard(
            [
                ["Статус", "Перепарсить"],
                ["Сохранить эталон", "Последнее изменение"],
                ["Пользователи", "Редакторы"],
                ["Удалить ДЗ", "Тестовая рассылка"],
                ["Закрыть админку"],
            ]
        )

    def welcome_text(is_editor: bool, is_admin: bool) -> str:
        lines = [
            f"Бот группы {settings.group_name}",
            "",
            "Используй кнопки ниже для расписания и домашних заданий.",
        ]
        if is_editor:
            lines.append("Кнопка «Добавить ДЗ» доступна тебе как редактору.")
        if is_admin:
            lines.append("Кнопка «Админка» доступна тебе как администратору.")
        return "\n".join(lines)

    async def settings_text(user_id: int, extra: str | None = None) -> str:
        user = await db.get_user("vk", user_id)
        linked = await db.get_linked_account("vk", user_id)
        notifications = "включены" if (user.homework_notifications_enabled if user else True) else "выключены"
        lines = [
            "Настройки",
            "",
            f"Уведомления о новом ДЗ: {notifications}",
        ]
        if linked:
            lines.append(f"Telegram привязан: {linked['linked_user_id']}")
        else:
            lines.append("Telegram пока не привязан.")
            lines.append("Открой Telegram-бота, создай код привязки и отправь его сюда.")
        if extra:
            lines.extend(["", extra])
        return "\n".join(lines)

    def schedule_text(day, fallback: str) -> str:
        if day is None or not day.lessons:
            label = fallback if day is None else day.date_label
            return f"Расписание на {label}\n\nПар нет."
        lines = [f"Расписание на {day.date_label}", ""]
        for lesson in day.lessons:
            lines.append(f"{lesson.number}. в {lesson.classroom} по {lesson.subject} у {lesson.teacher}")
        return "\n".join(lines)

    def homework_text(entry: dict, success_title: str | None = None) -> str:
        created_at = datetime.fromisoformat(entry["created_at"])
        lines = []
        if success_title:
            lines.extend([success_title, ""])
        lines.extend(
            [
                f"{entry['subject']} - {entry['teacher']}",
                f"#{entry['id']} | {entry['created_by_name']}",
                "-------------",
                entry["text"],
                "-------------",
                created_at.strftime("%d.%m.%Y %H:%M"),
            ]
        )
        return "\n".join(lines)

    def preview_text(draft: HomeworkDraft, author: str) -> str:
        return "\n".join(
            [
                f"{draft.subject_name} - {draft.teacher_name}",
                f"предпросмотр | {author}",
                "-------------",
                draft.text,
                "-------------",
                "Будет сохранено после подтверждения",
            ]
        )

    def snapshot_line(title: str, snapshot: dict | None) -> str:
        if snapshot is None:
            return f"{title}: еще не было"
        return f"{title}: {snapshot['created_at']}\nСайт отдал данные: {snapshot['fetched_at']}"

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

    async def show_main_menu(peer_id: int, user_id: int) -> None:
        peer_modes[peer_id] = "main_menu"
        is_editor = await user_is_editor(user_id)
        is_admin = user_is_admin(user_id)
        await show_screen(
            peer_id,
            welcome_text(is_editor, is_admin),
            keyboard=menu_keyboard(is_editor, is_admin),
        )

    async def show_settings(peer_id: int, user_id: int, extra: str | None = None, awaiting_code: bool = False) -> None:
        user = await db.get_user("vk", user_id)
        linked = await db.get_linked_account("vk", user_id)
        peer_modes[peer_id] = "settings_link_wait" if awaiting_code else "settings"
        await show_screen(
            peer_id,
            await settings_text(user_id, extra=extra),
            keyboard=settings_keyboard(
                linked=linked is not None,
                notifications_enabled=user.homework_notifications_enabled if user else True,
                awaiting_code=awaiting_code,
            ),
        )

    async def show_homework_subjects(peer_id: int, page: int = 0) -> None:
        labels = [subject["subject"] for subject in SUBJECTS]
        rows, actual_page = paged_rows(labels, page)
        peer_pages[peer_id]["homework_subjects"] = actual_page
        rows.append(["Назад в меню"])
        peer_modes[peer_id] = "homework_subjects"
        await show_screen(peer_id, "Выбери предмет. После выбора я покажу последнее домашнее задание.", keyboard=make_keyboard(rows))

    async def show_dz_subjects(peer_id: int, page: int = 0) -> None:
        labels = [subject["subject"] for subject in SUBJECTS]
        rows, actual_page = paged_rows(labels, page)
        peer_pages[peer_id]["dz_subjects"] = actual_page
        rows.append(["Назад в меню"])
        peer_modes[peer_id] = "dz_subjects"
        await show_screen(peer_id, "Выбери предмет для нового домашнего задания.", keyboard=make_keyboard(rows))

    async def show_admin_delete_subjects(peer_id: int, page: int = 0) -> None:
        labels = [subject["subject"] for subject in SUBJECTS]
        rows, actual_page = paged_rows(labels, page)
        peer_pages[peer_id]["admin_delete_subjects"] = actual_page
        rows.append(["Назад в админку"])
        peer_modes[peer_id] = "admin_delete_subjects"
        await show_screen(peer_id, "Удаление домашнего задания\n\nВыбери предмет, чтобы увидеть последние записи.", keyboard=make_keyboard(rows))

    async def show_latest_homework(peer_id: int, subject_key: str) -> None:
        subject = get_subject(subject_key)
        if subject is None:
            await show_screen(peer_id, "Предмет не найден.", keyboard=homework_view_keyboard())
            return
        entries = await db.get_homework_for_subject(subject_key)
        peer_modes[peer_id] = "homework_entry"
        if not entries:
            await show_screen(peer_id, f"По предмету {subject['subject']} пока нет домашних заданий.", keyboard=homework_view_keyboard())
            return
        entry = entries[0]
        await show_screen(
            peer_id,
            homework_text(entry),
            keyboard=homework_view_keyboard(),
            attachment=await collect_vk_attachments(peer_id, entry["attachments"]),
        )

    async def show_draft_preview(peer_id: int, author: str, draft: HomeworkDraft) -> None:
        peer_modes[peer_id] = "dz_preview"
        await show_screen(
            peer_id,
            preview_text(draft, author),
            keyboard=draft_preview_keyboard(),
            attachment=await collect_vk_attachments(peer_id, draft.attachments or []),
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
            "subject": draft.subject_name,
            "teacher": draft.teacher_name,
            "text": draft.text,
            "created_by_name": author,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "attachments": [
                {
                    "file_id": item.file_id,
                    "file_type": item.file_type,
                    "file_name": item.file_name,
                    "mime_type": item.mime_type,
                    "storage_path": item.storage_path,
                    "source_platform": item.source_platform,
                }
                for item in (draft.attachments or [])
            ],
        }
        homework_drafts.pop(user_id, None)
        await show_screen(
            peer_id,
            homework_text(entry, success_title="Домашнее задание успешно создано"),
            keyboard=menu_keyboard(await user_is_editor(user_id), user_is_admin(user_id)),
            attachment=await collect_vk_attachments(peer_id, draft.attachments or []),
        )
        if broadcaster is not None:
            await broadcaster.broadcast_homework_update(format_homework_notification(entry))

    def subject_by_title(title: str) -> dict[str, str] | None:
        normalized = title.strip().casefold()
        for subject in SUBJECTS:
            if subject["subject"].casefold() == normalized:
                return subject
        return None

    def build_editor_keyboard(peer_id: int, users: list, page: int) -> str:
        labels: dict[str, int] = {}
        button_texts: list[str] = []
        for user in users:
            if user.platform != "vk":
                continue
            display = user.full_name or user.username or str(user.user_id)
            prefix = "Снять ред." if user.is_editor else "Выдать ред."
            label = shorten_button_label(f"{prefix}: {display} ({user.user_id})")
            labels[label] = user.user_id
            button_texts.append(label)
        rows, actual_page = paged_rows(button_texts, page)
        peer_pages[peer_id]["editors"] = actual_page
        rows.append(["Назад в админку"])
        editor_option_map[peer_id] = labels
        return make_keyboard(rows)

    def build_delete_keyboard(peer_id: int, subject_key: str, entries: list[dict], page: int) -> str:
        labels: dict[str, tuple[int, str]] = {}
        button_texts: list[str] = []
        for entry in entries:
            preview = entry["text"].strip().replace("\n", " ")
            label = shorten_button_label(f"Удалить #{entry['id']} {preview or 'без текста'}")
            labels[label] = (entry["id"], subject_key)
            button_texts.append(label)
        rows, actual_page = paged_rows(button_texts, page)
        peer_pages[peer_id]["delete_entries"] = actual_page
        rows.append(["Назад к предметам"])
        rows.append(["Назад в админку"])
        delete_option_map[peer_id] = labels
        return make_keyboard(rows)

    @bot.on.message()
    async def all_messages_handler(message: Message) -> None:
        await register_user(message)
        if message.peer_id is None or message.from_id is None:
            return

        peer_id = message.peer_id
        user_id = message.from_id
        text = (message.text or "").strip()
        normalized = text.casefold()
        mode = peer_modes.get(peer_id, "main_menu")
        draft = homework_drafts.get(user_id)

        if normalized in {"/start", "start", "начать"}:
            homework_drafts.pop(user_id, None)
            await show_main_menu(peer_id, user_id)
            return

        if text in {"Назад в меню", "Закрыть админку"}:
            homework_drafts.pop(user_id, None)
            await show_main_menu(peer_id, user_id)
            return

        if text == "Настройки":
            await show_settings(peer_id, user_id)
            return

        if text == "Выключить уведомления о ДЗ":
            await db.set_homework_notifications("vk", user_id, False)
            await show_settings(peer_id, user_id, extra="Уведомления о ДЗ выключены.")
            return

        if text == "Включить уведомления о ДЗ":
            await db.set_homework_notifications("vk", user_id, True)
            await show_settings(peer_id, user_id, extra="Уведомления о ДЗ включены.")
            return

        if text == "У меня есть код привязки":
            await show_settings(peer_id, user_id, extra="Отправь сюда код из Telegram одним сообщением.", awaiting_code=True)
            return

        if text == "Отменить ввод кода":
            await show_settings(peer_id, user_id)
            return

        if text == "Отвязать Telegram":
            deleted = await db.unlink_account("vk", user_id)
            await show_settings(peer_id, user_id, extra="Привязка удалена." if deleted else "Привязки не было.")
            return

        token_candidate = text.strip().upper()
        if mode == "settings_link_wait" and len(token_candidate) == 6 and token_candidate.isalnum():
            success, response = await db.consume_link_token(token_candidate, "vk", user_id)
            await show_settings(peer_id, user_id, extra=response)
            return

        if text in {"/rasp", "Расписание"}:
            peer_modes[peer_id] = "schedule_menu"
            await show_screen(peer_id, "Выбери нужный вариант расписания.", keyboard=schedule_keyboard())
            return

        if mode == "schedule_menu":
            snapshot = await db.get_latest_snapshot("current")
            if snapshot is None:
                await show_screen(peer_id, "Сохраненное расписание пока отсутствует.", keyboard=schedule_keyboard())
                return
            if text == "Расписание на сегодня":
                await show_screen(peer_id, schedule_text(get_day_by_offset_from_content(snapshot["content"], 0), "сегодня"), keyboard=schedule_keyboard())
                return
            if text == "Расписание на завтра":
                await show_screen(peer_id, schedule_text(get_day_by_offset_from_content(snapshot["content"], 1), "завтра"), keyboard=schedule_keyboard())
                return
            if text == "Расписание на 2 дня":
                await show_screen(peer_id, schedule_text(get_day_by_offset_from_content(snapshot["content"], 2), "2 дня"), keyboard=schedule_keyboard())
                return

        if text in {"/homework", "Домашние задания"}:
            await show_homework_subjects(peer_id)
            return

        if text == "Вернуться к списку ДЗ":
            await show_homework_subjects(peer_id, peer_pages[peer_id].get("homework_subjects", 0))
            return

        if mode == "homework_subjects":
            if text == "Следующая страница":
                await show_homework_subjects(peer_id, peer_pages[peer_id].get("homework_subjects", 0) + 1)
                return
            if text == "Предыдущая страница":
                await show_homework_subjects(peer_id, peer_pages[peer_id].get("homework_subjects", 0) - 1)
                return
            subject = subject_by_title(text)
            if subject is not None:
                await show_latest_homework(peer_id, subject["key"])
                return

        if text in {"/dz", "Добавить ДЗ"}:
            if not await user_is_editor(user_id):
                await show_screen(peer_id, "Эта кнопка доступна только редакторам домашнего задания.", keyboard=menu_keyboard(await user_is_editor(user_id), user_is_admin(user_id)))
                return
            await show_dz_subjects(peer_id)
            return

        if text == "Отменить":
            homework_drafts.pop(user_id, None)
            await show_main_menu(peer_id, user_id)
            return

        if mode == "dz_subjects":
            if text == "Следующая страница":
                await show_dz_subjects(peer_id, peer_pages[peer_id].get("dz_subjects", 0) + 1)
                return
            if text == "Предыдущая страница":
                await show_dz_subjects(peer_id, peer_pages[peer_id].get("dz_subjects", 0) - 1)
                return
            subject = subject_by_title(text)
            if subject is not None:
                homework_drafts[user_id] = HomeworkDraft(
                    subject_key=subject["key"],
                    subject_name=subject["subject"],
                    teacher_name=subject["teacher"],
                )
                peer_modes[peer_id] = "dz_text"
                await show_screen(peer_id, f"Выбран предмет {subject['subject']}.\n\nТеперь отправь текст домашнего задания одним сообщением.", keyboard=make_keyboard([["Отменить"]]))
                return

        if draft is not None and draft.awaiting_text and text and text not in {"Добавить вложения", "Опубликовать"}:
            draft.text = text
            draft.awaiting_text = False
            draft.awaiting_attachments = False
            await show_draft_preview(peer_id, str(user_id), draft)
            return

        if text == "Добавить вложения":
            if draft is None or not draft.text.strip():
                await show_screen(peer_id, "Сначала добавь текст домашнего задания.", keyboard=draft_preview_keyboard())
                return
            draft.awaiting_attachments = True
            peer_modes[peer_id] = "dz_attachments"
            await show_screen(peer_id, "Отправь вложения сообщениями: документ, фото, видео или аудио.\n\nПосле каждого файла я обновлю предпросмотр. Когда закончишь, нажми «Опубликовать».", keyboard=draft_attachment_keyboard())
            return

        if draft is not None and draft.awaiting_attachments:
            full_message = await message.get_full_message()
            attachments = (
                await attachment_storage.save_vk_message_attachments(full_message)
                if attachment_storage is not None
                else []
            )
            if not attachments:
                attachment_strings = full_message.get_attachment_strings() or []
                attachments = [
                    HomeworkAttachment(
                        file_id=attachment,
                        file_type="vk_attachment",
                        file_name=None,
                        mime_type=None,
                        source_platform="vk",
                    )
                    for attachment in attachment_strings
                ]
            if attachments:
                draft.attachments.extend(attachments)
                await show_draft_preview(peer_id, str(user_id), draft)
                return
            if text == "Опубликовать" and draft.text.strip():
                await publish_homework(peer_id, user_id, str(user_id))
                return
            await show_screen(peer_id, "Сейчас я жду вложение. Отправь документ, фото, видео или аудио, либо нажми «Опубликовать».", keyboard=draft_attachment_keyboard())
            return

        if text == "Опубликовать":
            if draft is None or not draft.text.strip():
                await show_screen(peer_id, "Нет готового черновика для публикации.", keyboard=menu_keyboard(await user_is_editor(user_id), user_is_admin(user_id)))
                return
            await publish_homework(peer_id, user_id, str(user_id))
            return

        if text in {"/admin", "Админка"}:
            if not user_is_admin(user_id):
                await show_screen(peer_id, "Эта кнопка доступна только администратору.", keyboard=menu_keyboard(await user_is_editor(user_id), user_is_admin(user_id)))
                return
            peer_modes[peer_id] = "admin_menu"
            await show_screen(peer_id, "Админ-панель\n\nВыбери нужное действие.", keyboard=admin_keyboard())
            return

        if user_is_admin(user_id):
            if text == "Назад в админку":
                peer_modes[peer_id] = "admin_menu"
                await show_screen(peer_id, "Админ-панель\n\nВыбери нужное действие.", keyboard=admin_keyboard())
                return
            if text == "Статус":
                await show_screen(peer_id, await admin_status_text(), keyboard=admin_keyboard())
                return
            if text == "Перепарсить":
                snapshot, snapshot_hash = await parser.parse()
                await db.save_snapshot("current", snapshot_hash, snapshot)
                await show_screen(peer_id, "Расписание перепарсено\n\n" + schedule_text(get_day_by_offset(snapshot, 0), "сегодня"), keyboard=admin_keyboard())
                return
            if text == "Сохранить эталон":
                snapshot, snapshot_hash = await parser.parse()
                await db.save_snapshot("daily_baseline", snapshot_hash, snapshot)
                await show_screen(peer_id, "Эталон для сравнения сохранен\n\n" + schedule_text(get_day_by_offset(snapshot, 0), "сегодня"), keyboard=admin_keyboard())
                return
            if text == "Последнее изменение":
                last_change = await db.get_last_change()
                response = "Последнее изменение\n\nИзменений пока не было." if not last_change else f"Последнее изменение\n\n{last_change['created_at']}\n\n{last_change['message']}"
                await show_screen(peer_id, response, keyboard=admin_keyboard())
                return
            if text == "Пользователи":
                users = await db.list_users()
                await sync_vk_user_names([user.user_id for user in users if user.platform == "vk"])
                users = await db.list_users()
                lines = ["Пользователи", ""]
                if not users:
                    lines.append("Пока никто не зарегистрирован.")
                else:
                    for user in users:
                        display = user.full_name or user.username or "Без имени"
                        roles = []
                        if user.is_admin:
                            roles.append("админ")
                        if user.is_editor:
                            roles.append("редактор")
                        role_text = f" ({', '.join(roles)})" if roles else ""
                        lines.append(f"- {user.platform} | {display} | {user.user_id}{role_text}")
                await show_screen(peer_id, "\n".join(lines), keyboard=make_keyboard([["Назад в админку"]]))
                return
            if text == "Редакторы":
                users = await db.list_users("vk")
                await sync_vk_user_names([user.user_id for user in users])
                users = await db.list_users("vk")
                peer_modes[peer_id] = "admin_editors"
                await show_screen(peer_id, "Управление редакторами\n\nВыбери пользователя, чтобы выдать или снять роль редактора.", keyboard=build_editor_keyboard(peer_id, users, 0))
                return
            if mode == "admin_editors":
                users = await db.list_users("vk")
                await sync_vk_user_names([user.user_id for user in users])
                users = await db.list_users("vk")
                if text == "Следующая страница":
                    await show_screen(peer_id, "Управление редакторами\n\nВыбери пользователя, чтобы выдать или снять роль редактора.", keyboard=build_editor_keyboard(peer_id, users, peer_pages[peer_id].get("editors", 0) + 1))
                    return
                if text == "Предыдущая страница":
                    await show_screen(peer_id, "Управление редакторами\n\nВыбери пользователя, чтобы выдать или снять роль редактора.", keyboard=build_editor_keyboard(peer_id, users, peer_pages[peer_id].get("editors", 0) - 1))
                    return
                if text in editor_option_map.get(peer_id, {}):
                    target_id = editor_option_map[peer_id][text]
                    target = await db.get_user("vk", target_id)
                    if target is not None:
                        await db.set_editor("vk", target_id, not target.is_editor)
                    users = await db.list_users("vk")
                    await show_screen(peer_id, "Управление редакторами\n\nРоль обновлена. Выбери пользователя, чтобы продолжить.", keyboard=build_editor_keyboard(peer_id, users, peer_pages[peer_id].get("editors", 0)))
                    return
            if text == "Удалить ДЗ":
                await show_admin_delete_subjects(peer_id)
                return
            if mode == "admin_delete_subjects":
                if text == "Следующая страница":
                    await show_admin_delete_subjects(peer_id, peer_pages[peer_id].get("admin_delete_subjects", 0) + 1)
                    return
                if text == "Предыдущая страница":
                    await show_admin_delete_subjects(peer_id, peer_pages[peer_id].get("admin_delete_subjects", 0) - 1)
                    return
                subject = subject_by_title(text)
                if subject is not None:
                    entries = await db.get_homework_for_subject(subject["key"])
                    peer_modes[peer_id] = "admin_delete_entries"
                    if not entries:
                        await show_screen(peer_id, f"{subject['subject']}\n\nДля этого предмета пока нет записей.", keyboard=make_keyboard([["Назад к предметам"], ["Назад в админку"]]))
                    else:
                        await show_screen(peer_id, f"{subject['subject']}\n\nВыбери запись, которую нужно удалить.", keyboard=build_delete_keyboard(peer_id, subject["key"], entries, 0))
                    return
            if text == "Назад к предметам":
                await show_admin_delete_subjects(peer_id)
                return
            if mode == "admin_delete_entries":
                mapping = delete_option_map.get(peer_id, {})
                subject_key = next(iter(mapping.values()))[1] if mapping else None
                if text == "Следующая страница" and subject_key:
                    entries = await db.get_homework_for_subject(subject_key)
                    subject = get_subject(subject_key)
                    await show_screen(peer_id, f"{subject['subject']}\n\nВыбери запись, которую нужно удалить.", keyboard=build_delete_keyboard(peer_id, subject_key, entries, peer_pages[peer_id].get("delete_entries", 0) + 1))
                    return
                if text == "Предыдущая страница" and subject_key:
                    entries = await db.get_homework_for_subject(subject_key)
                    subject = get_subject(subject_key)
                    await show_screen(peer_id, f"{subject['subject']}\n\nВыбери запись, которую нужно удалить.", keyboard=build_delete_keyboard(peer_id, subject_key, entries, peer_pages[peer_id].get("delete_entries", 0) - 1))
                    return
                if text in mapping:
                    homework_id, subject_key = mapping[text]
                    deleted = await db.delete_homework(homework_id)
                    entries = await db.get_homework_for_subject(subject_key)
                    subject = get_subject(subject_key)
                    subject_name = subject["subject"] if subject else "Предмет"
                    if not entries:
                        await show_screen(peer_id, f"{subject_name}\n\n" + ("Запись удалена." if deleted else "Запись не найдена."), keyboard=make_keyboard([["Назад к предметам"], ["Назад в админку"]]))
                    else:
                        await show_screen(peer_id, f"{subject_name}\n\n" + ("Запись удалена.\n\nВыбери следующую запись для удаления." if deleted else "Запись не найдена.\n\nВыбери следующую запись для удаления."), keyboard=build_delete_keyboard(peer_id, subject_key, entries, peer_pages[peer_id].get("delete_entries", 0)))
                    return
            if text == "Тестовая рассылка":
                users = await db.get_users_for_platform("vk")
                for user in users:
                    try:
                        await bot.api.messages.send(peer_ids=[user.user_id], message="Тестовое уведомление: бот активен и рассылка работает.", random_id=0)
                    except Exception:
                        continue
                await show_screen(peer_id, "Тестовая рассылка\n\nСообщение отправлено всем зарегистрированным пользователям VK.", keyboard=admin_keyboard())
                return

        await show_main_menu(peer_id, user_id)

    return bot
