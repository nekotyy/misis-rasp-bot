from __future__ import annotations

from collections import defaultdict
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, PhotoSize, Video

from src.config import Settings
from src.db import Database
from src.homework_service import SUBJECTS, format_homework_message, format_homework_preview, get_subject
from src.models import HomeworkAttachment, HomeworkDraft
from src.notifier import Broadcaster
from src.parser import ScheduleParser
from src.schedule_service import ScheduleFormatter, get_day_by_offset, get_day_by_offset_from_content


SCHEDULE_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Расписание на сегодня", callback_data="schedule:today")],
        [InlineKeyboardButton(text="Расписание на завтра", callback_data="schedule:tomorrow")],
        [InlineKeyboardButton(text="Расписание на 2 дня", callback_data="schedule:day_after")],
        [InlineKeyboardButton(text="Назад", callback_data="menu:start")],
    ]
)

HOMEWORK_BACK_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Вернуться к предметам", callback_data="menu:homework")],
    ]
)

ADMIN_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Статус", callback_data="admin:status"),
            InlineKeyboardButton(text="Перепарсить", callback_data="admin:refresh"),
        ],
        [
            InlineKeyboardButton(text="Пользователи", callback_data="admin:users"),
            InlineKeyboardButton(text="Редакторы", callback_data="admin:editors"),
        ],
        [
            InlineKeyboardButton(text="Тестовая рассылка", callback_data="admin:test"),
            InlineKeyboardButton(text="Последнее изменение", callback_data="admin:last_change"),
        ],
        [InlineKeyboardButton(text="Закрыть админку", callback_data="admin:close")],
    ]
)

HOMEWORK_PREVIEW_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Добавить вложения", callback_data="dz:add_attachments")],
        [InlineKeyboardButton(text="Сохранить", callback_data="dz:save")],
        [InlineKeyboardButton(text="Отмена", callback_data="dz:cancel")],
    ]
)

HOMEWORK_ATTACHMENT_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Сохранить", callback_data="dz:save")],
        [InlineKeyboardButton(text="Отмена", callback_data="dz:cancel")],
    ]
)


def build_dispatcher(
    settings: Settings,
    db: Database,
    parser: ScheduleParser,
    broadcaster: Broadcaster | None = None,
) -> Dispatcher:
    dispatcher = Dispatcher()
    homework_drafts: dict[int, HomeworkDraft] = {}
    context_messages: dict[int, dict[str, list[int]]] = defaultdict(dict)

    def build_homework_subjects_keyboard(mode: str) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        for subject in SUBJECTS:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=subject["subject"],
                        callback_data=(
                            f"homework:view:{subject['key']}"
                            if mode == "homework"
                            else f"dz:subject:{subject['key']}"
                        ),
                    )
                ]
            )
        rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:start")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def build_editors_keyboard(users: list) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        for user in users:
            if user.platform != "telegram":
                continue
            display = user.full_name or user.username or str(user.user_id)
            prefix = "Убрать редактора" if user.is_editor else "Сделать редактором"
            rows.append(
                [InlineKeyboardButton(text=f"{prefix}: {display}", callback_data=f"editor:toggle:{user.user_id}")]
            )
        rows.append([InlineKeyboardButton(text="Назад в админку", callback_data="admin:back")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def register_message_user(message: Message) -> None:
        user = message.from_user
        if user is None:
            return
        full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip() or None
        is_admin = bool(settings.admin_telegram_id and user.id == settings.admin_telegram_id)
        existing = await db.get_user("telegram", user.id)
        await db.upsert_user(
            platform="telegram",
            user_id=user.id,
            username=user.username,
            full_name=full_name,
            is_admin=is_admin,
            is_editor=existing.is_editor if existing else False,
        )

    async def register_callback_user(callback: CallbackQuery) -> None:
        user = callback.from_user
        full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip() or None
        existing = await db.get_user("telegram", user.id)
        await db.upsert_user(
            platform="telegram",
            user_id=user.id,
            username=user.username,
            full_name=full_name,
            is_admin=user_is_admin(user.id),
            is_editor=existing.is_editor if existing else False,
        )

    def user_is_admin(user_id: int | None) -> bool:
        return bool(user_id and settings.admin_telegram_id and user_id == settings.admin_telegram_id)

    async def user_is_editor(user_id: int | None) -> bool:
        if user_id is None:
            return False
        user = await db.get_user("telegram", user_id)
        return bool(user and user.is_editor)

    async def get_saved_snapshot() -> dict | None:
        return await db.get_latest_snapshot("current")

    def format_welcome(is_editor: bool = False) -> str:
        editor_hint = "\n/dz — добавить домашнее задание" if is_editor else ""
        return (
            f"<b>Привет! Я бот группы {escape(settings.group_name)}</b>\n\n"
            "/rasp — посмотреть расписание\n"
            "/homework — посмотреть домашние задания"
            f"{editor_hint}"
        )

    def format_admin_panel() -> str:
        return (
            "<b>Админ-панель</b>\n\n"
            "Здесь можно перепарсить сайт, посмотреть пользователей, выдать редактора и проверить состояние бота."
        )

    def empty_day_text(label: str) -> str:
        return f"Расписание на {escape(label)}\n\nПар нет."

    async def replace_context_message(
        bot: Bot,
        chat_id: int,
        context: str,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        message_ids = context_messages[chat_id].get(context, [])
        if message_ids:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_ids[0],
                    text=text,
                    reply_markup=reply_markup,
                )
                for extra_id in message_ids[1:]:
                    try:
                        await bot.delete_message(chat_id=chat_id, message_id=extra_id)
                    except TelegramBadRequest:
                        pass
                context_messages[chat_id][context] = [message_ids[0]]
                return
            except TelegramBadRequest:
                pass
        await clear_context_messages(bot, chat_id, context)
        sent = await bot.send_message(chat_id, text, reply_markup=reply_markup)
        context_messages[chat_id][context] = [sent.message_id]

    async def clear_context_messages(bot: Bot, chat_id: int, context: str) -> None:
        for message_id in context_messages[chat_id].get(context, []):
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except TelegramBadRequest:
                pass
        context_messages[chat_id][context] = []

    async def clear_context_messages_except(bot: Bot, chat_id: int, context: str, keep_message_id: int) -> None:
        kept_ids: list[int] = []
        for message_id in context_messages[chat_id].get(context, []):
            if message_id == keep_message_id:
                kept_ids.append(message_id)
                continue
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except TelegramBadRequest:
                pass
        context_messages[chat_id][context] = kept_ids

    async def send_schedule_menu(bot: Bot, chat_id: int) -> None:
        await replace_context_message(
            bot,
            chat_id,
            "schedule",
            "Выбери нужный вариант расписания.",
            reply_markup=SCHEDULE_KEYBOARD,
        )

    async def send_homework_subject_picker(bot: Bot, chat_id: int, mode: str) -> None:
        text = (
            "<b>Выбери предмет</b>\n\nПосле выбора я покажу домашние задания."
            if mode == "homework"
            else "<b>Выбери предмет для нового домашнего задания</b>"
        )
        await replace_context_message(
            bot,
            chat_id,
            "homework" if mode == "homework" else "dz",
            text,
            reply_markup=build_homework_subjects_keyboard(mode),
        )

    async def send_homework_entries(
        bot: Bot,
        chat_id: int,
        subject_key: str,
        source_message: Message | None = None,
    ) -> None:
        subject = get_subject(subject_key)
        if subject is None:
            if source_message is not None:
                await source_message.edit_text("Предмет не найден.", reply_markup=HOMEWORK_BACK_KEYBOARD)
                context_messages[chat_id]["homework"] = [source_message.message_id]
            else:
                await replace_context_message(
                    bot,
                    chat_id,
                    "homework",
                    "Предмет не найден.",
                    reply_markup=HOMEWORK_BACK_KEYBOARD,
                )
            return

        entries = await db.get_homework_for_subject(subject_key)
        if not entries:
            if source_message is not None:
                await clear_context_messages_except(bot, chat_id, "homework", source_message.message_id)
                await source_message.edit_text(
                    f"По предмету <b>{escape(subject['subject'])}</b> пока нет домашних заданий.",
                    reply_markup=HOMEWORK_BACK_KEYBOARD,
                )
                context_messages[chat_id]["homework"] = [source_message.message_id]
            else:
                await replace_context_message(
                    bot,
                    chat_id,
                    "homework",
                    f"По предмету <b>{escape(subject['subject'])}</b> пока нет домашних заданий.",
                    reply_markup=HOMEWORK_BACK_KEYBOARD,
                )
            return

        if source_message is not None:
            await clear_context_messages_except(bot, chat_id, "homework", source_message.message_id)
            await source_message.edit_text(
                f"<b>Домашние задания: {escape(subject['subject'])}</b>\n\nПоказываю последние записи.",
                reply_markup=HOMEWORK_BACK_KEYBOARD,
            )
            sent_ids = [source_message.message_id]
        else:
            await replace_context_message(
                bot,
                chat_id,
                "homework",
                f"<b>Домашние задания: {escape(subject['subject'])}</b>\n\nПоказываю последние записи.",
                reply_markup=HOMEWORK_BACK_KEYBOARD,
            )
            sent_ids = context_messages[chat_id]["homework"][:1]

        for entry in entries:
            entry_message_ids = await send_homework_entry_with_attachments(bot, chat_id, entry)
            sent_ids.extend(entry_message_ids)

        context_messages[chat_id]["homework"] = sent_ids

    async def send_homework_entry_with_attachments(bot: Bot, chat_id: int, entry: dict) -> list[int]:
        message_text = format_homework_message(entry)
        attachments = entry["attachments"]
        sent_ids: list[int] = []
        if not attachments:
            sent = await bot.send_message(chat_id, message_text)
            sent_ids.append(sent.message_id)
            return sent_ids

        first, *rest = attachments
        first_sent = await send_attachment(bot, chat_id, first, caption=message_text)
        if first_sent is not None:
            sent_ids.append(first_sent.message_id)
        for attachment in rest:
            extra_sent = await send_attachment(
                bot,
                chat_id,
                attachment,
                caption=attachment.get("file_name") or "Вложение",
            )
            if extra_sent is not None:
                sent_ids.append(extra_sent.message_id)
        return sent_ids

    async def send_attachment(bot: Bot, chat_id: int, attachment: dict, caption: str | None = None):
        file_type = attachment["file_type"]
        file_id = attachment["file_id"]
        if file_type == "photo":
            return await bot.send_photo(chat_id, photo=file_id, caption=caption)
        if file_type == "video":
            return await bot.send_video(chat_id, video=file_id, caption=caption)
        if file_type == "audio":
            return await bot.send_audio(chat_id, audio=file_id, caption=caption)
        return await bot.send_document(chat_id, document=file_id, caption=caption)

    async def send_draft_preview(message: Message, draft: HomeworkDraft) -> None:
        author = message.from_user.full_name if message.from_user else "Неизвестный пользователь"
        await replace_context_message(
            message.bot,
            message.chat.id,
            "dz",
            format_homework_preview(
                subject_name=draft.subject_name,
                teacher_name=draft.teacher_name,
                text=draft.text,
                attachments=draft.attachments or [],
                created_by_name=author,
            ),
            reply_markup=HOMEWORK_PREVIEW_KEYBOARD,
        )

    @dispatcher.message(CommandStart())
    async def handle_start(message: Message) -> None:
        await register_message_user(message)
        editor = await user_is_editor(message.from_user.id if message.from_user else None)
        await replace_context_message(message.bot, message.chat.id, "menu", format_welcome(is_editor=editor))

    @dispatcher.message(Command("rasp"))
    async def handle_rasp_command(message: Message) -> None:
        await register_message_user(message)
        await send_schedule_menu(message.bot, message.chat.id)

    @dispatcher.message(Command("homework"))
    async def handle_homework_command(message: Message) -> None:
        await register_message_user(message)
        await send_homework_subject_picker(message.bot, message.chat.id, "homework")

    @dispatcher.message(Command("dz"))
    async def handle_dz_command(message: Message) -> None:
        await register_message_user(message)
        if not await user_is_editor(message.from_user.id if message.from_user else None):
            await replace_context_message(message.bot, message.chat.id, "dz", "Команда доступна только редакторам домашнего задания.")
            return
        await send_homework_subject_picker(message.bot, message.chat.id, "dz")

    @dispatcher.message(Command("cancel"))
    async def handle_cancel_command(message: Message) -> None:
        await register_message_user(message)
        if message.from_user:
            homework_drafts.pop(message.from_user.id, None)
        await clear_context_messages(message.bot, message.chat.id, "dz")
        await replace_context_message(message.bot, message.chat.id, "menu", format_welcome(is_editor=await user_is_editor(message.from_user.id if message.from_user else None)))

    @dispatcher.message(Command("admin"))
    async def handle_admin_command(message: Message) -> None:
        await register_message_user(message)
        if not user_is_admin(message.from_user.id if message.from_user else None):
            await replace_context_message(message.bot, message.chat.id, "admin", "Команда доступна только администратору.")
            return
        await replace_context_message(message.bot, message.chat.id, "admin", format_admin_panel(), ADMIN_KEYBOARD)

    @dispatcher.callback_query(F.data == "menu:start")
    async def handle_menu_start(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        editor = await user_is_editor(callback.from_user.id)
        if callback.message is not None:
            try:
                await callback.message.edit_text(format_welcome(is_editor=editor))
                context_messages[callback.message.chat.id]["menu"] = [callback.message.message_id]
            except TelegramBadRequest:
                await replace_context_message(callback.bot, callback.message.chat.id, "menu", format_welcome(is_editor=editor))
        await callback.answer()

    @dispatcher.callback_query(F.data == "menu:homework")
    async def handle_menu_homework(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        await send_homework_subject_picker(callback.bot, callback.from_user.id, "homework")
        await callback.answer()

    @dispatcher.callback_query(F.data.startswith("schedule:"))
    async def handle_schedule_callback(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        if callback.message is None:
            await callback.answer()
            return
        snapshot_row = await get_saved_snapshot()
        if snapshot_row is None:
            await callback.message.edit_text(
                "Сохраненное расписание пока отсутствует. Подожди первую автоматическую синхронизацию.",
                reply_markup=SCHEDULE_KEYBOARD,
            )
            await callback.answer()
            return

        action = callback.data.split(":", 1)[1]
        if action == "today":
            day = get_day_by_offset_from_content(snapshot_row["content"], 0)
            text = ScheduleFormatter.format_day_card(day, "сегодня") if day else empty_day_text("сегодня")
        elif action == "tomorrow":
            day = get_day_by_offset_from_content(snapshot_row["content"], 1)
            text = ScheduleFormatter.format_day_card(day, "завтра") if day else empty_day_text("завтра")
        else:
            day = get_day_by_offset_from_content(snapshot_row["content"], 2)
            text = ScheduleFormatter.format_day_card(day, "2 дня") if day else empty_day_text("2 дня")

        await callback.message.edit_text(text, reply_markup=SCHEDULE_KEYBOARD)
        context_messages[callback.message.chat.id]["schedule"] = [callback.message.message_id]
        await callback.answer()

    @dispatcher.callback_query(F.data.startswith("homework:view:"))
    async def handle_homework_subject(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        await callback.answer()
        await send_homework_entries(
            callback.bot,
            callback.from_user.id,
            callback.data.split(":")[-1],
            source_message=callback.message,
        )

    @dispatcher.callback_query(F.data.startswith("dz:subject:"))
    async def handle_homework_subject_for_create(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        if not await user_is_editor(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        subject = get_subject(callback.data.split(":")[-1])
        if subject is None:
            await callback.answer("Предмет не найден.", show_alert=True)
            return
        homework_drafts[callback.from_user.id] = HomeworkDraft(
            subject_key=subject["key"],
            subject_name=subject["subject"],
            teacher_name=subject["teacher"],
        )
        if callback.message is not None:
            await callback.message.edit_text(
                f"Выбран предмет <b>{escape(subject['subject'])}</b>.\n\nТеперь отправь текст домашнего задания одним сообщением.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="dz:cancel")]]
                ),
            )
            context_messages[callback.message.chat.id]["dz"] = [callback.message.message_id]
        await callback.answer()

    @dispatcher.callback_query(F.data == "dz:add_attachments")
    async def handle_add_attachments(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        draft = homework_drafts.get(callback.from_user.id)
        if draft is None:
            await callback.answer("Черновик не найден.", show_alert=True)
            return
        draft.awaiting_attachments = True
        if callback.message is not None:
            await callback.message.edit_text(
                "Отправь вложения сообщениями: документ, фото, видео или аудио.\n\nКогда закончишь, нажми «Сохранить».",
                reply_markup=HOMEWORK_ATTACHMENT_KEYBOARD,
            )
            context_messages[callback.message.chat.id]["dz"] = [callback.message.message_id]
        await callback.answer()

    @dispatcher.callback_query(F.data == "dz:save")
    async def handle_save_homework(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        draft = homework_drafts.get(callback.from_user.id)
        if draft is None or not draft.text.strip():
            await callback.answer("Нет готового черновика для сохранения.", show_alert=True)
            return
        author = callback.from_user.full_name or callback.from_user.username or str(callback.from_user.id)
        homework_id = await db.create_homework(
            subject_key=draft.subject_key,
            subject=draft.subject_name,
            teacher=draft.teacher_name,
            text=draft.text,
            created_by_platform="telegram",
            created_by_user_id=callback.from_user.id,
            created_by_name=author,
            attachments=draft.attachments or [],
        )
        homework_drafts.pop(callback.from_user.id, None)
        await replace_context_message(
            callback.bot,
            callback.from_user.id,
            "dz",
            f"Домашнее задание сохранено.\n\nID записи: <b>{homework_id}</b>",
        )
        await callback.answer("Сохранено")

    @dispatcher.callback_query(F.data == "dz:cancel")
    async def handle_cancel_homework(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        homework_drafts.pop(callback.from_user.id, None)
        editor = await user_is_editor(callback.from_user.id)
        await replace_context_message(callback.bot, callback.from_user.id, "menu", format_welcome(is_editor=editor))
        await clear_context_messages(callback.bot, callback.from_user.id, "dz")
        await callback.answer("Отменено")

    @dispatcher.callback_query(F.data.startswith("admin:"))
    async def handle_admin_callback(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        if not user_is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if callback.message is None:
            await callback.answer()
            return

        action = callback.data.split(":", 1)[1]
        if action == "close":
            editor = await user_is_editor(callback.from_user.id)
            await callback.message.edit_text(format_welcome(is_editor=editor))
            context_messages[callback.message.chat.id]["menu"] = [callback.message.message_id]
            await callback.answer()
            return
        if action == "back":
            await callback.message.edit_text(format_admin_panel(), reply_markup=ADMIN_KEYBOARD)
            await callback.answer()
            return

        if action == "status":
            users = await db.list_users()
            last_change = await db.get_last_change()
            last_change_at = escape(last_change["created_at"]) if last_change else "пока не было"
            text = (
                "<b>Статус бота</b>\n\n"
                f"Группа: <b>{escape(settings.group_name)}</b>\n"
                f"Пользователей: <b>{len(users)}</b>\n"
                f"Последнее изменение: <b>{last_change_at}</b>"
            )
            reply_markup = ADMIN_KEYBOARD
        elif action == "users":
            users = await db.list_users()
            if not users:
                text = "<b>Пользователи</b>\n\nПока никто не зарегистрирован."
            else:
                lines = ["<b>Пользователи</b>", ""]
                for user in users:
                    display = user.full_name or user.username or "Без имени"
                    roles = []
                    if user.is_admin:
                        roles.append("админ")
                    if user.is_editor:
                        roles.append("редактор")
                    role_text = f" ({', '.join(roles)})" if roles else ""
                    lines.append(f"- {escape(user.platform)} | {escape(display)} | <b>{user.user_id}</b>{escape(role_text)}")
                text = "\n".join(lines)
            reply_markup = ADMIN_KEYBOARD
        elif action == "editors":
            users = [user for user in await db.list_users("telegram")]
            text = "<b>Управление редакторами</b>\n\nНажми на пользователя, чтобы выдать или снять роль редактора."
            reply_markup = build_editors_keyboard(users)
        elif action == "last_change":
            last_change = await db.get_last_change()
            if not last_change:
                text = "<b>Последнее изменение</b>\n\nИзменений пока не было."
            else:
                text = (
                    "<b>Последнее изменение</b>\n\n"
                    f"<b>{escape(last_change['created_at'])}</b>\n\n"
                    f"{escape(last_change['message'])}"
                )
            reply_markup = ADMIN_KEYBOARD
        elif action == "refresh":
            snapshot, snapshot_hash = await parser.parse()
            await db.save_snapshot("current", snapshot_hash, snapshot)
            day = get_day_by_offset(snapshot, 0)
            preview = ScheduleFormatter.format_day_card(day, "сегодня") if day else empty_day_text("сегодня")
            text = "<b>Расписание перепарсено</b>\n\n" + preview
            reply_markup = ADMIN_KEYBOARD
        else:
            if broadcaster is not None:
                await broadcaster.broadcast_test_message()
            text = "<b>Тестовая рассылка</b>\n\nСообщение отправлено всем зарегистрированным пользователям."
            reply_markup = ADMIN_KEYBOARD

        await callback.message.edit_text(text, reply_markup=reply_markup)
        context_messages[callback.message.chat.id]["admin"] = [callback.message.message_id]
        await callback.answer()

    @dispatcher.callback_query(F.data.startswith("editor:toggle:"))
    async def handle_editor_toggle(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        if not user_is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        user_id = int(callback.data.split(":")[-1])
        target = await db.get_user("telegram", user_id)
        if target is None:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return
        await db.set_editor("telegram", user_id, not target.is_editor)
        users = [user for user in await db.list_users("telegram")]
        await callback.message.edit_text(
            "<b>Управление редакторами</b>\n\nНажми на пользователя, чтобы выдать или снять роль редактора.",
            reply_markup=build_editors_keyboard(users),
        )
        await callback.answer("Роль обновлена")

    @dispatcher.message(F.document | F.photo | F.video | F.audio)
    async def handle_homework_attachment_message(message: Message) -> None:
        await register_message_user(message)
        if message.from_user is None:
            return
        draft = homework_drafts.get(message.from_user.id)
        if draft is None or not draft.awaiting_attachments:
            return

        attachment: HomeworkAttachment | None = None
        if message.document:
            attachment = HomeworkAttachment(
                file_id=message.document.file_id,
                file_type="document",
                file_name=message.document.file_name,
                mime_type=message.document.mime_type,
            )
        elif message.photo:
            photo: PhotoSize = message.photo[-1]
            attachment = HomeworkAttachment(
                file_id=photo.file_id,
                file_type="photo",
                file_name="photo.jpg",
                mime_type="image/jpeg",
            )
        elif message.video:
            video: Video = message.video
            attachment = HomeworkAttachment(
                file_id=video.file_id,
                file_type="video",
                file_name=video.file_name,
                mime_type=video.mime_type,
            )
        elif message.audio:
            attachment = HomeworkAttachment(
                file_id=message.audio.file_id,
                file_type="audio",
                file_name=message.audio.file_name,
                mime_type=message.audio.mime_type,
            )

        if attachment is None:
            return

        draft.attachments.append(attachment)
        await replace_context_message(
            message.bot,
            message.chat.id,
            "dz",
            f"Вложение добавлено. Сейчас в черновике <b>{len(draft.attachments)}</b> вложений.",
            reply_markup=HOMEWORK_ATTACHMENT_KEYBOARD,
        )

    @dispatcher.message(F.text == "Домашние задания")
    async def handle_homework_text_shortcut(message: Message) -> None:
        await register_message_user(message)
        await send_homework_subject_picker(message.bot, message.chat.id, "homework")

    @dispatcher.message(F.text)
    async def handle_text_message(message: Message) -> None:
        await register_message_user(message)
        if message.from_user is None or message.text is None:
            return
        draft = homework_drafts.get(message.from_user.id)
        if draft is not None and draft.awaiting_text:
            draft.text = message.text.strip()
            draft.awaiting_text = False
            draft.awaiting_attachments = False
            await send_draft_preview(message, draft)
            return

        await replace_context_message(
            message.bot,
            message.chat.id,
            "menu",
            "Используй /rasp для расписания, /homework для просмотра ДЗ и /dz для добавления домашки, если у тебя есть роль редактора.",
        )

    return dispatcher
