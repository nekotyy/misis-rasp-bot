from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.config import Settings
from src.db import Database
from src.group_catalog import GroupCatalog
from src.message_broker import OutboundMessage, RabbitMQBroker
from src.notifier import CAMPAIGN_ADMIN_BROADCAST
from src.parser import ScheduleParser
from web_configurator.lesson_editor import load_lesson_config, save_lesson_config, validate_lesson_config
from web_configurator.metrics import collect_metrics
from web_configurator.security import ALL_PERMISSIONS, PERMISSION_LABELS, SessionSigner, WebAuthStore, WebUser


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
load_dotenv(ENV_PATH)

settings = Settings.from_env()
started_at = datetime.now()
auth_store = WebAuthStore(
    Path(os.getenv("WEB_USERS_PATH", "storage/web_users.json")).resolve(),
    os.getenv("WEB_SUPERUSER_LOGIN", "admin"),
    os.getenv("WEB_SUPERUSER_PASSWORD", ""),
)
signer = SessionSigner(os.getenv("WEB_CONFIG_SECRET", "change-me"))

app = FastAPI(title="MISIS bot configurator")

PERMISSION_DESCRIPTIONS = {
    "stats_overview": "Главные цифры на первой строке дашборда: аптайм, аудитория, новые пользователи и общая активность.",
    "stats_users": "Таблица пользователей Telegram/VK, фильтры по платформе, подписке и свежести регистрации.",
    "stats_services": "Состояние Telegram, VK и RabbitMQ без технических деталей ошибок.",
    "stats_schedule": "Последний парсинг, активные группы и журнал изменений расписания.",
    "stats_delivery": "Сколько сообщений доставлено сегодня и за все время: RabbitMQ, напрямую, Telegram и VK.",
    "stats_lesson_counters": "Метрики счетчиков пар: сколько настроено, что учитывалось сегодня, текущий прогресс.",
    "config_lesson_counters": "Доступ к редактору счетчиков пар: группы, дисциплины, преподаватели, JSON-режим.",
    "manage_bot_admin": "Рассылки, перепарсинг, сохранение эталона и управление редакторами бота прямо из вебки.",
    "manage_web_users": "Создание, удаление и изменение прав пользователей веб-админки.",
}


def current_user(request: Request) -> WebUser:
    login = signer.unsign(request.cookies.get("web_config_session"))
    user = auth_store.get_user(login or "")
    if user is None:
        raise HTTPException(status_code=401)
    return user


def require(permission: str):
    def dependency(user: Annotated[WebUser, Depends(current_user)]) -> WebUser:
        if user.is_superuser or permission in user.permissions:
            return user
        raise HTTPException(status_code=403, detail="Недостаточно прав.")

    return dependency


@app.exception_handler(401)
async def unauthorized_handler(_: Request, __: HTTPException) -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def index(user: Annotated[WebUser, Depends(current_user)]) -> str:
    return layout("Панель", dashboard_html(user), user)


@app.get("/login", response_class=HTMLResponse)
async def login_form() -> str:
    return base_page(
        "Вход",
        f"""
        <main class="login">
          <form method="post" action="/login" class="panel narrow">
            <div class="brand" style="margin-bottom:18px"><span class="brand-mark">{icon("spark")}</span><div><b>MISIS Control</b><small>secure dashboard</small></div></div>
            <label>Логин <input name="login" autocomplete="username" required></label>
            <label>Пароль <input name="password" type="password" autocomplete="current-password" required></label>
            <button type="submit">{icon("shield")} Войти</button>
          </form>
        </main>
        """,
    )


@app.post("/login")
async def login(login: Annotated[str, Form()], password: Annotated[str, Form()]) -> RedirectResponse:
    user = auth_store.authenticate(login, password)
    if user is None:
        return RedirectResponse("/login?error=1", status_code=303)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("web_config_session", signer.sign(user.login), httponly=True, samesite="lax")
    return response


@app.post("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("web_config_session")
    return response


@app.get("/api/metrics")
async def api_metrics(user: Annotated[WebUser, Depends(current_user)]):
    fresh_settings = Settings.from_env()
    await Database(fresh_settings.database_path).initialize()
    metrics = await collect_metrics(
        fresh_settings.database_path,
        rabbitmq_url=fresh_settings.rabbitmq_url,
        telegram_token=fresh_settings.telegram_bot_token,
        vk_token=fresh_settings.vk_bot_token,
        started_at=started_at,
    )
    return filter_metrics_for_user(metrics, user)


@app.get("/lessons", response_class=HTMLResponse)
async def lessons_page(user: Annotated[WebUser, Depends(require("config_lesson_counters"))]) -> str:
    payload = load_lesson_config(Settings.from_env().lesson_counters_path)
    return layout("Счетчики пар", lessons_manager_html(payload), user)


@app.post("/lessons/json", response_class=HTMLResponse)
async def save_lessons_json(
    user: Annotated[WebUser, Depends(require("config_lesson_counters"))],
    payload: Annotated[str, Form()],
    mode: Annotated[str, Form()] = "validate",
) -> str:
    try:
        raw_payload = parse_json_payload(payload)
        group_catalog = GroupCatalog(Settings.from_env().schedule_url)
        await group_catalog.ensure_loaded()
        parser = ScheduleParser(Settings.from_env().schedule_url)
        normalized, problems = await validate_lesson_config(raw_payload, group_catalog=group_catalog, parser=parser)
        saved = False
        if mode == "save" and not any(problem["level"] == "error" for problem in problems):
            save_lesson_config(Settings.from_env().lesson_counters_path, normalized)
            saved = True
        editor_value = json_dumps(normalized if saved else raw_payload)
        report = problems_html(problems, saved)
    except Exception as exc:
        editor_value = payload
        report = f"<div class='alert bad'>Ошибка: {html_escape(str(exc))}</div>"

    try:
        display_payload = parse_json_payload(editor_value)
    except Exception:
        display_payload = load_lesson_config(Settings.from_env().lesson_counters_path)
    content = lessons_manager_html(display_payload, report=report, raw_json=editor_value)
    return layout("Счетчики пар", content, user)


@app.post("/lessons/groups")
async def add_lesson_group(
    _: Annotated[WebUser, Depends(require("config_lesson_counters"))],
    group_name: Annotated[str, Form()] = "",
    schedule_id: Annotated[str, Form()] = "",
) -> RedirectResponse:
    payload = load_lesson_config(Settings.from_env().lesson_counters_path)
    group_catalog = GroupCatalog(Settings.from_env().schedule_url)
    await group_catalog.ensure_loaded()
    resolved_id, resolved_name = await resolve_group_input(group_catalog, group_name, schedule_id)
    if resolved_id is None:
        return RedirectResponse("/lessons?error=group", status_code=303)
    groups = payload.setdefault("groups", [])
    if not any(safe_int(item.get("schedule_id")) == resolved_id for item in groups if isinstance(item, dict)):
        groups.append({"schedule_id": resolved_id, "group_name": resolved_name or str(resolved_id), "subjects": []})
        save_lesson_config(Settings.from_env().lesson_counters_path, payload)
    return RedirectResponse("/lessons", status_code=303)


@app.post("/lessons/groups/delete")
async def delete_lesson_group(
    _: Annotated[WebUser, Depends(require("config_lesson_counters"))],
    schedule_id: Annotated[int, Form()],
) -> RedirectResponse:
    payload = load_lesson_config(Settings.from_env().lesson_counters_path)
    payload["groups"] = [
        item for item in payload.get("groups", [])
        if not (isinstance(item, dict) and safe_int(item.get("schedule_id")) == schedule_id)
    ]
    save_lesson_config(Settings.from_env().lesson_counters_path, payload)
    return RedirectResponse("/lessons", status_code=303)


@app.post("/lessons/subjects")
async def upsert_lesson_subject(
    user: Annotated[WebUser, Depends(require("config_lesson_counters"))],
    schedule_id: Annotated[int, Form()],
    subject: Annotated[str, Form()],
    teacher: Annotated[str, Form()],
    passed: Annotated[int, Form()] = 0,
    total: Annotated[int, Form()] = 0,
    original_subject: Annotated[str, Form()] = "",
    original_teacher: Annotated[str, Form()] = "",
):
    payload = load_lesson_config(Settings.from_env().lesson_counters_path)
    group = find_config_group(payload, schedule_id)
    if group is None:
        return RedirectResponse("/lessons?error=group", status_code=303)
    subjects = group.setdefault("subjects", [])
    replacement = {"subject": subject.strip(), "teacher": teacher.strip(), "passed": max(0, passed), "total": max(0, total)}
    replaced = False
    if original_subject or original_teacher:
        for index, item in enumerate(subjects):
            if item.get("subject") == original_subject and item.get("teacher") == original_teacher:
                subjects[index] = replacement
                replaced = True
                break
    if not replaced:
        subjects.append(replacement)
    normalized, problems = await validate_payload_for_save(payload)
    if any(problem["level"] == "error" for problem in problems):
        return HTMLResponse(layout("Счетчики пар", lessons_manager_html(payload, report=problems_html(problems, False)), user))
    save_lesson_config(Settings.from_env().lesson_counters_path, normalized)
    return RedirectResponse("/lessons", status_code=303)


@app.post("/lessons/subjects/delete")
async def delete_lesson_subject(
    _: Annotated[WebUser, Depends(require("config_lesson_counters"))],
    schedule_id: Annotated[int, Form()],
    subject: Annotated[str, Form()],
    teacher: Annotated[str, Form()],
) -> RedirectResponse:
    payload = load_lesson_config(Settings.from_env().lesson_counters_path)
    group = find_config_group(payload, schedule_id)
    if group is not None:
        group["subjects"] = [
            item for item in group.get("subjects", [])
            if not (item.get("subject") == subject and item.get("teacher") == teacher)
        ]
        save_lesson_config(Settings.from_env().lesson_counters_path, payload)
    return RedirectResponse("/lessons", status_code=303)


@app.get("/web-users", response_class=HTMLResponse)
async def web_users_page(user: Annotated[WebUser, Depends(require("manage_web_users"))]) -> str:
    return layout("Веб-пользователи", web_users_html(), user)


@app.post("/web-users")
async def save_web_user(
    _: Annotated[WebUser, Depends(require("manage_web_users"))],
    login: Annotated[str, Form()],
    password: Annotated[str, Form()] = "",
    permissions: Annotated[list[str], Form()] = [],
) -> RedirectResponse:
    auth_store.upsert_user(login, password or None, permissions)
    return RedirectResponse("/web-users", status_code=303)


@app.post("/web-users/delete")
async def delete_web_user(
    _: Annotated[WebUser, Depends(require("manage_web_users"))],
    login: Annotated[str, Form()],
) -> RedirectResponse:
    auth_store.delete_user(login)
    return RedirectResponse("/web-users", status_code=303)


@app.get("/control", response_class=HTMLResponse)
async def control_page(user: Annotated[WebUser, Depends(require("manage_bot_admin"))]) -> str:
    return layout("Управление ботом", await bot_control_html(), user)


@app.post("/control/broadcast", response_class=HTMLResponse)
async def control_broadcast(
    user: Annotated[WebUser, Depends(require("manage_bot_admin"))],
    message: Annotated[str, Form()],
) -> str:
    text = message.strip()
    if not text:
        return layout("Управление ботом", await bot_control_html("<div class='alert bad'>Текст рассылки не должен быть пустым.</div>"), user)
    try:
        sent = await enqueue_broadcast(text)
    except Exception as exc:
        return layout("Управление ботом", await bot_control_html(f"<div class='alert bad'>Не удалось поставить рассылку в очередь: {html_escape(exc)}</div>"), user)
    return layout("Управление ботом", await bot_control_html(f"<div class='alert good'>Рассылка поставлена в очередь. Получателей: {sent}.</div>"), user)


@app.post("/control/test", response_class=HTMLResponse)
async def control_test_broadcast(user: Annotated[WebUser, Depends(require("manage_bot_admin"))]) -> str:
    try:
        sent = await enqueue_broadcast("Тестовое уведомление: бот активен и рассылка работает.")
    except Exception as exc:
        return layout("Управление ботом", await bot_control_html(f"<div class='alert bad'>Тестовую рассылку не удалось отправить: {html_escape(exc)}</div>"), user)
    return layout("Управление ботом", await bot_control_html(f"<div class='alert good'>Тестовая рассылка поставлена в очередь. Получателей: {sent}.</div>"), user)


@app.post("/control/refresh", response_class=HTMLResponse)
async def control_refresh(user: Annotated[WebUser, Depends(require("manage_bot_admin"))]) -> str:
    rows = await refresh_all_active_sources()
    return layout("Управление ботом", await bot_control_html(action_report_html("Перепарсинг активных источников", rows, "перепарсено")), user)


@app.post("/control/baseline", response_class=HTMLResponse)
async def control_baseline(user: Annotated[WebUser, Depends(require("manage_bot_admin"))]) -> str:
    rows = await save_baseline_for_all_active_sources()
    return layout("Управление ботом", await bot_control_html(action_report_html("Сохранение эталонов", rows, "эталон сохранен")), user)


@app.post("/control/editors", response_class=HTMLResponse)
async def control_editor_toggle(
    user: Annotated[WebUser, Depends(require("manage_bot_admin"))],
    platform: Annotated[str, Form()],
    user_id: Annotated[int, Form()],
) -> str:
    db = await get_db()
    target = await db.get_user(platform, user_id)
    if target is None:
        return layout("Управление ботом", await bot_control_html("<div class='alert bad'>Пользователь не найден.</div>"), user)
    await db.set_editor(platform, user_id, not target.is_editor)
    label = "выдана" if not target.is_editor else "снята"
    return layout("Управление ботом", await bot_control_html(f"<div class='alert good'>Роль редактора {label} для {html_escape(platform)}:{user_id}.</div>"), user)


def lessons_manager_html(payload: dict[str, Any], report: str = "", raw_json: str | None = None) -> str:
    groups = [item for item in payload.get("groups", []) if isinstance(item, dict)]
    group_cards = "\n".join(lesson_group_card(group) for group in groups)
    if not group_cards:
        group_cards = "<div class='empty-state'>Пока нет групп. Добавь первую группу по названию или schedule_id.</div>"
    raw_json = raw_json if raw_json is not None else json_dumps(payload)
    return f"""
    <section class="page-head compact-head">
      <div>
        <p class="eyebrow">Семестр</p>
        <h2>Счетчики пройденных пар</h2>
        <p>Основной режим — карточки и формы. JSON оставлен как расширенный режим для быстрых массовых правок.</p>
      </div>
      <div class="head-chips">
        <span class="chip">Групп: {len(groups)}</span>
        <span class="chip">JSON доступен</span>
      </div>
    </section>
    {report}
    <section class="panel">
      <div class="panel-title"><span class="icon">{icon("calendar")}</span><h3>Добавить группу</h3></div>
      <form method="post" action="/lessons/groups" class="inline-form">
        <label>Группа <input name="group_name" placeholder="ИСП-25-1"></label>
        <label>schedule_id <input name="schedule_id" inputmode="numeric" placeholder="600"></label>
        <button type="submit">{icon("save")} Добавить</button>
      </form>
    </section>
    <section class="lesson-grid">{group_cards}</section>
    <details class="panel json-details">
      <summary>{icon("file-json")} Расширенный режим JSON</summary>
      <form method="post" action="/lessons/json" class="json-form">
        <textarea name="payload" spellcheck="false">{html_escape(raw_json)}</textarea>
        <div class="actions">
          <button class="secondary" name="mode" value="validate" type="submit">{icon("scan")} Проверить</button>
          <button name="mode" value="save" type="submit">{icon("save")} Проверить и сохранить JSON</button>
        </div>
      </form>
    </details>
    """


def lesson_group_card(group: dict[str, Any]) -> str:
    schedule_id = int(group.get("schedule_id", 0) or 0)
    group_name = str(group.get("group_name") or schedule_id)
    subjects = [item for item in group.get("subjects", []) if isinstance(item, dict)]
    subject_rows = "\n".join(lesson_subject_row(schedule_id, item) for item in subjects)
    if not subject_rows:
        subject_rows = "<div class='empty-state small'>В этой группе пока нет дисциплин.</div>"
    return f"""
    <article class="panel lesson-card">
      <div class="lesson-card-head">
        <div>
          <h3>{html_escape(group_name)}</h3>
          <div class="chips"><span class="chip">ID {schedule_id}</span><span class="chip">{len(subjects)} дисциплин</span></div>
        </div>
        <form method="post" action="/lessons/groups/delete">
          <input type="hidden" name="schedule_id" value="{schedule_id}">
          <button class="ghost danger" type="submit">{icon("trash")}</button>
        </form>
      </div>
      <div class="subject-list">{subject_rows}</div>
      <form method="post" action="/lessons/subjects" class="subject-form">
        <input type="hidden" name="schedule_id" value="{schedule_id}">
        <label>Дисциплина <input name="subject" placeholder="Информатика" required></label>
        <label>Преподаватель <input name="teacher" placeholder="Иванов И. И." required></label>
        <label>Прошло <input name="passed" type="number" min="0" value="0"></label>
        <label>Всего <input name="total" type="number" min="0" value="0"></label>
        <button type="submit">{icon("save")} Добавить дисциплину</button>
      </form>
    </article>
    """


def lesson_subject_row(schedule_id: int, item: dict[str, Any]) -> str:
    subject = str(item.get("subject") or "")
    teacher = str(item.get("teacher") or "")
    passed = int(item.get("passed", 0) or 0)
    total = int(item.get("total", 0) or 0)
    return f"""
    <details class="subject-row">
      <summary>
        <span><b>{html_escape(subject)}</b><small>{html_escape(teacher)}</small></span>
        <span class="chip">{passed}/{total}</span>
      </summary>
      <form method="post" action="/lessons/subjects" class="subject-form edit">
        <input type="hidden" name="schedule_id" value="{schedule_id}">
        <input type="hidden" name="original_subject" value="{html_escape(subject)}">
        <input type="hidden" name="original_teacher" value="{html_escape(teacher)}">
        <label>Дисциплина <input name="subject" value="{html_escape(subject)}" required></label>
        <label>Преподаватель <input name="teacher" value="{html_escape(teacher)}" required></label>
        <label>Прошло <input name="passed" type="number" min="0" value="{passed}"></label>
        <label>Всего <input name="total" type="number" min="0" value="{total}"></label>
        <button type="submit">{icon("save")} Сохранить</button>
      </form>
      <form method="post" action="/lessons/subjects/delete" class="delete-line">
        <input type="hidden" name="schedule_id" value="{schedule_id}">
        <input type="hidden" name="subject" value="{html_escape(subject)}">
        <input type="hidden" name="teacher" value="{html_escape(teacher)}">
        <button class="ghost danger" type="submit">{icon("trash")} Удалить дисциплину</button>
      </form>
    </details>
    """


async def resolve_group_input(group_catalog: GroupCatalog, group_name: str, schedule_id: str) -> tuple[int | None, str | None]:
    if schedule_id.strip():
        try:
            resolved_id = int(schedule_id.strip())
        except ValueError:
            return None, None
        group = await group_catalog.get_by_schedule_id(resolved_id)
        return resolved_id, group.group_name if group else group_name.strip()
    if group_name.strip():
        group = await group_catalog.find_group(group_name.strip())
        if group is not None:
            return group.schedule_id, group.group_name
    return None, None


def find_config_group(payload: dict[str, Any], schedule_id: int) -> dict[str, Any] | None:
    for item in payload.get("groups", []):
        if isinstance(item, dict) and safe_int(item.get("schedule_id")) == schedule_id:
            return item
    return None


def safe_int(value: object, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def validate_payload_for_save(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    group_catalog = GroupCatalog(Settings.from_env().schedule_url)
    await group_catalog.ensure_loaded()
    parser = ScheduleParser(Settings.from_env().schedule_url)
    return await validate_lesson_config(payload, group_catalog=group_catalog, parser=parser)


async def get_db() -> Database:
    fresh_settings = Settings.from_env()
    db = Database(fresh_settings.database_path)
    await db.initialize()
    return db


async def enqueue_broadcast(message: str) -> int:
    fresh_settings = Settings.from_env()
    if not fresh_settings.rabbitmq_url:
        raise RuntimeError("RabbitMQ не настроен.")
    db = await get_db()
    broker = RabbitMQBroker(
        url=fresh_settings.rabbitmq_url,
        queue_name=fresh_settings.rabbitmq_queue,
        prefetch_count=fresh_settings.rabbitmq_prefetch_count,
    )
    telegram_users = await db.get_users_for_notifications("telegram")
    vk_users = await db.get_users_for_notifications("vk")
    sent = 0
    try:
        for user in telegram_users:
            await broker.publish(
                OutboundMessage(
                    platform="telegram",
                    user_id=user.user_id,
                    text=message,
                    campaign_type=CAMPAIGN_ADMIN_BROADCAST,
                )
            )
            sent += 1
        for user in vk_users:
            await broker.publish(
                OutboundMessage(
                    platform="vk",
                    user_id=user.user_id,
                    text=message,
                    campaign_type=CAMPAIGN_ADMIN_BROADCAST,
                )
            )
            sent += 1
    finally:
        await broker.close()
    return sent


async def refresh_all_active_sources() -> list[tuple[str, str, str]]:
    db = await get_db()
    sources = await db.get_active_sources()
    if not sources:
        return []
    parser = ScheduleParser(Settings.from_env().schedule_url)
    rows: list[tuple[str, str, str]] = []
    for source in sources:
        try:
            if source["source_type"] == "teacher" and source["source_url"]:
                snapshot, snapshot_hash = await parser.parse_from_url(str(source["source_url"]))
            else:
                snapshot, snapshot_hash = await parser.parse(int(source["schedule_id"]))
            await db.save_snapshot(
                "current",
                snapshot_hash,
                snapshot,
                source["schedule_id"],
                source["group_name"],
                source_type=source["source_type"],
                source_key=source["source_key"],
                source_title=source["source_title"],
                source_url=source["source_url"],
            )
            rows.append((str(source["source_title"]), snapshot.fetched_at.strftime("%Y-%m-%d %H:%M"), "перепарсено"))
        except Exception as exc:
            rows.append((str(source["source_title"]), datetime.now().strftime("%Y-%m-%d %H:%M"), f"ошибка: {type(exc).__name__}"))
    return rows


async def save_baseline_for_all_active_sources() -> list[tuple[str, str, str]]:
    db = await get_db()
    sources = await db.get_active_sources()
    if not sources:
        return []
    parser = ScheduleParser(Settings.from_env().schedule_url)
    rows: list[tuple[str, str, str]] = []
    for source in sources:
        try:
            if source["source_type"] == "teacher" and source["source_url"]:
                snapshot, snapshot_hash = await parser.parse_from_url(str(source["source_url"]))
            else:
                snapshot, snapshot_hash = await parser.parse(int(source["schedule_id"]))
            await db.save_snapshot(
                "daily_baseline",
                snapshot_hash,
                snapshot,
                source["schedule_id"],
                source["group_name"],
                source_type=source["source_type"],
                source_key=source["source_key"],
                source_title=source["source_title"],
                source_url=source["source_url"],
            )
            rows.append((str(source["source_title"]), snapshot.fetched_at.strftime("%Y-%m-%d %H:%M"), "эталон сохранен"))
        except Exception as exc:
            rows.append((str(source["source_title"]), datetime.now().strftime("%Y-%m-%d %H:%M"), f"ошибка: {type(exc).__name__}"))
    return rows


def action_report_html(title: str, rows: list[tuple[str, str, str]], success_label: str) -> str:
    if not rows:
        return f"<div class='alert'>{html_escape(title)}: активных источников пока нет.</div>"
    items = "".join(
        f"<tr><td>{html_escape(source)}</td><td>{html_escape(action_time)}</td><td>{html_escape(action_name or success_label)}</td></tr>"
        for source, action_time, action_name in rows
    )
    return (
        f"<div class='alert good'>{html_escape(title)} завершено. Источников: {len(rows)}.</div>"
        f"<div class='table-wrap'><table><tr><th>Источник</th><th>Когда</th><th>Результат</th></tr>{items}</table></div>"
    )


async def bot_control_html(report: str = "") -> str:
    db = await get_db()
    users = await db.list_users()
    editors = [user for user in users if not user.is_admin]
    group_rows = await db.get_group_user_stats()
    changes = await db.get_daily_change_groups(datetime.now().date().isoformat())
    active_sources = await db.get_active_sources()
    delivery_stats = await db.get_delivery_stats()
    group_table = "".join(
        f"<tr><td>{index}</td><td>{html_escape(row['group_name'])}</td><td>{int(row['users_count'])}</td></tr>"
        for index, row in enumerate(group_rows, start=1)
    ) or "<tr><td colspan='3'>Пока нет пользователей с группами.</td></tr>"
    change_table = "".join(
        f"<tr><td>{html_escape(row['group_name'])}</td><td>{html_escape(row['created_at'])}</td></tr>"
        for row in changes
    ) or "<tr><td colspan='2'>За сегодня изменений пока не было.</td></tr>"
    editor_cards = "\n".join(bot_editor_row(user) for user in editors) or "<div class='empty-state'>Пока нет пользователей бота для управления ролями.</div>"
    source_count = len(active_sources)
    teacher_count = sum(1 for item in active_sources if item.get("source_type") == "teacher")
    group_count = sum(1 for item in active_sources if item.get("source_type") == "group")
    return f"""
    <section class="page-head compact-head">
      <div>
        <p class="eyebrow">Операции</p>
        <h2>Управление ботом</h2>
        <p>Веб-панель для массовых действий: рассылка, перепарсинг активных источников, эталоны и редакторы.</p>
      </div>
      <div class="head-chips">
        <span class="chip">Источников: {source_count}</span>
        <span class="chip">Групп: {group_count}</span>
        <span class="chip">Преподавателей: {teacher_count}</span>
      </div>
    </section>
    {report}
    <section class="dashboard-block">
      <div class="metric-grid compact">
        <div class="card"><span>Пользователей</span><b>{len(users)}</b><small>TG/VK вместе</small></div>
        <div class="card"><span>Редакторов</span><b>{sum(1 for user in users if user.is_editor)}</b><small>без админов</small></div>
        <div class="card"><span>Рассылок</span><b>{delivery_stats.get('admin_broadcast_sent', 0)}</b><small>успешно за все время</small></div>
        <div class="card"><span>Служебных уведомлений</span><b>{delivery_stats.get('admin_notify_sent', 0)}</b><small>админу</small></div>
      </div>
    </section>
    <section class="panel">
      <div class="panel-title"><span class="icon">{icon('tools')}</span><h3>Быстрые действия</h3></div>
      <div class="action-grid">
        <form method="post" action="/control/refresh" class="action-card"><b>{icon('refresh')} Перепарсить</b><p>Обновить текущие слепки по всем активным источникам.</p><button type="submit">Запустить</button></form>
        <form method="post" action="/control/baseline" class="action-card"><b>{icon('layers')} Сохранить эталон</b><p>Записать свежий baseline для сегодняшних сравнений.</p><button type="submit">Сохранить</button></form>
        <form method="post" action="/control/test" class="action-card"><b>{icon('flask')} Тестовая рассылка</b><p>Поставить в очередь короткое тестовое сообщение всем получателям.</p><button type="submit">Отправить тест</button></form>
      </div>
    </section>
    <section class="panel">
      <div class="panel-title"><span class="icon">{icon('megaphone')}</span><h3>Рассылка</h3></div>
      <form method="post" action="/control/broadcast" class="broadcast-form">
        <label>Текст сообщения <textarea name="message" placeholder="Сообщение для всех пользователей с включенными уведомлениями." required></textarea></label>
        <button type="submit">{icon('send')} Поставить в очередь</button>
      </form>
    </section>
    <section class="panel">
      <div class="panel-title"><span class="icon">{icon('users')}</span><h3>Редакторы бота</h3></div>
      <div class="editor-grid">{editor_cards}</div>
    </section>
    <section class="two-col">
      <div class="panel">
        <div class="panel-title"><span class="icon">{icon('calendar')}</span><h3>Группы</h3></div>
        <div class="table-wrap"><table><tr><th>#</th><th>Группа</th><th>Юзеров</th></tr>{group_table}</table></div>
      </div>
      <div class="panel">
        <div class="panel-title"><span class="icon">{icon('alert')}</span><h3>Изменения за сегодня</h3></div>
        <div class="table-wrap"><table><tr><th>Источник</th><th>Когда</th></tr>{change_table}</table></div>
      </div>
    </section>
    """


def bot_editor_row(user: Any) -> str:
    display_name = user.full_name or (f"@{user.username}" if user.username else f"{user.platform}:{user.user_id}")
    profile_href = user_profile_link(user.platform, user.user_id, user.username)
    status_chip = "<span class='chip chip-ok'>редактор</span>" if user.is_editor else "<span class='chip'>без роли</span>"
    return f"""
    <article class="editor-card-row">
      <div>
        <a class="profile-link strong" href="{html_escape(profile_href)}" target="_blank" rel="noreferrer">{html_escape(display_name)}</a>
        <p class="muted">{html_escape(user.platform.upper())} · ID {user.user_id}</p>
      </div>
      <div class="chips">{status_chip}</div>
      <form method="post" action="/control/editors">
        <input type="hidden" name="platform" value="{html_escape(user.platform)}">
        <input type="hidden" name="user_id" value="{user.user_id}">
        <button class="secondary" type="submit">{icon('shield')} {"Снять роль" if user.is_editor else "Сделать редактором"}</button>
      </form>
    </article>
    """


def user_profile_link(platform: str, user_id: int, username: str | None) -> str:
    if platform == "vk":
        return f"https://vk.com/id{user_id}"
    if username:
        return f"https://t.me/{username.lstrip('@')}"
    return f"tg://user?id={user_id}"


def dashboard_html(user: WebUser) -> str:
    cards = []
    if can(user, "stats_overview"):
        cards.append(
            """
            <button class="to-top icon-only" type="button" onclick="window.scrollTo({top:0,behavior:'smooth'})" aria-label="Наверх" title="Наверх">
              __TO_TOP_ICON__
            </button>
            <section class="hero">
              <div>
                <p class="eyebrow">Системный монитор</p>
                <h2>Пульс бота расписания</h2>
                <p>Обновляется каждые 30 секунд. Смотри нагрузку, доставку, подписки и состояние счетчиков в одном месте.</p>
              </div>
            </section>
            <section class="dashboard-block"><div id="overview" class="metric-grid"></div></section>
            """
            .replace("__TO_TOP_ICON__", icon("arrow-up"))
        )
    if can(user, "stats_services"):
        cards.append(f"<section class='panel'><div class='panel-title'><span class='icon'>{icon('pulse')}</span><h3>Сервисы</h3></div><div id='services' class='service-grid'></div></section>")
    if can(user, "stats_users"):
        cards.append(
            f"""
            <details class="panel collapsible">
              <summary class="collapse-title"><span class="icon">{icon('users')}</span><h3>Пользователи</h3><span class="collapse-hint">Развернуть</span></summary>
              <div class="toolbar">
                <label class="search">{icon('search')}<input id="userSearch" placeholder="Имя, ID, группа, преподаватель"></label>
                <select id="platformFilter"><option value="">Все платформы</option><option value="telegram">Telegram</option><option value="vk">VK</option></select>
                <select id="kindFilter"><option value="">Все подписки</option><option value="teacher">Преподаватели</option><option value="group">Группы</option><option value="new">Новые</option><option value="old">Старые</option></select>
              </div>
              <div class="table-wrap"><table id="usersTable"></table></div>
            </details>
            """
        )
    if can(user, "stats_schedule"):
        cards.append(f"<details class='panel collapsible'><summary class='collapse-title'><span class='icon'>{icon('calendar')}</span><h3>Расписание и изменения</h3><span class='collapse-hint'>Развернуть</span></summary><div id='schedule'></div></details>")
    if can(user, "stats_delivery"):
        cards.append(f"<section class='panel'><div class='panel-title'><span class='icon'>{icon('send')}</span><h3>Доставка сообщений</h3></div><div id='delivery' class='metric-grid compact'></div></section>")
    if can(user, "stats_lesson_counters"):
        cards.append(f"<section class='panel'><div class='panel-title'><span class='icon'>{icon('counter')}</span><h3>Подсчет пар</h3></div><div id='lessonsStats'></div></section>")
    return "\n".join(cards) + DASHBOARD_SCRIPT


def web_users_html() -> str:
    permissions = permission_checklist([])
    rows = "\n".join(web_user_row(user) for user in auth_store.list_users())
    return f"""
    <section class="panel">
      <div class="panel-title"><span class="icon">{icon('shield')}</span><h3>Создать / обновить пользователя</h3></div>
      <form method="post" class="grid-form">
        <label>Логин <input name="login" required></label>
        <label>Пароль <input name="password" type="password" placeholder="оставь пустым, чтобы не менять"></label>
        <div class="checks">{permissions}</div>
        <button>{icon('save')} Сохранить</button>
      </form>
    </section>
    <section class="panel"><div class="panel-title"><span class="icon">{icon('users')}</span><h3>Пользователи</h3></div><div class="user-admin-list">{rows}</div></section>
    """


def web_user_row(user: dict[str, Any]) -> str:
    login = html_escape(user["login"])
    if user["is_superuser"]:
        return f"""
        <article class="admin-user-card">
          <div><h3>{login}</h3><div class="chips"><span class="chip">суперюзер</span><span class="chip">все права</span></div></div>
        </article>
        """
    return f"""
    <article class="admin-user-card">
      <form method="post" class="admin-user-form">
        <input type="hidden" name="login" value="{login}">
        <div class="admin-user-head">
          <div><h3>{login}</h3><p class="muted">Пароль можно оставить пустым, тогда изменятся только права.</p></div>
          <label class="password-inline">Новый пароль <input name="password" type="password" placeholder="не менять"></label>
        </div>
        <div class="checks">{permission_checklist(user["permissions"])}</div>
        <div class="actions">
          <button>{icon('save')} Сохранить права</button>
          {delete_form(user["login"])}
        </div>
      </form>
    </article>
    """


def permission_checklist(selected: list[str]) -> str:
    selected_set = set(selected)
    return "\n".join(
        f"""
        <label class='check'>
          <input type='checkbox' name='permissions' value='{permission}' {'checked' if permission in selected_set else ''}>
          <span>{html_escape(PERMISSION_LABELS[permission])}</span>
          <span class='help' tabindex='0'>{icon('help')}<span class='tooltip'>{html_escape(PERMISSION_DESCRIPTIONS[permission])}</span></span>
        </label>
        """
        for permission in ALL_PERMISSIONS
    )


def delete_form(login: str) -> str:
    return f"<button class='ghost danger' type='submit' formaction='/web-users/delete'>{icon('trash')} Удалить</button>"


def layout(title: str, content: str, user: WebUser) -> str:
    nav_items = [nav_link("/", "Метрики", "dashboard")]
    if can(user, "config_lesson_counters"):
        nav_items.append(nav_link("/lessons", "Счетчики пар", "counter"))
    if can(user, "manage_bot_admin"):
        nav_items.append(nav_link("/control", "Управление ботом", "tools"))
    if can(user, "manage_web_users"):
        nav_items.append(nav_link("/web-users", "Веб-пользователи", "shield"))
    nav = "".join(nav_items)
    return base_page(
        title,
        f"""
        <div class="shell">
          <aside class="sidebar">
            <div class="brand"><span class="brand-mark">{icon('spark')}</span><div><b>MISIS Control</b><small>bot dashboard</small></div></div>
            <nav>{nav}</nav>
            <form method="post" action="/logout" class="logout"><button class="ghost">{icon('logout')} Выйти</button></form>
          </aside>
          <div class="workspace">
            <header class="topbar">
              <div><p class="eyebrow">Панель управления</p><h1>{title}</h1></div>
              <div class="user-pill">{icon('user')} {html_escape(user.login)}</div>
            </header>
            <main>{content}</main>
          </div>
        </div>
        """,
    )


def base_page(title: str, body: str) -> str:
    return f"<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html_escape(title)}</title>{STYLE}</head><body>{body}</body></html>"


def nav_link(href: str, label: str, icon_name: str) -> str:
    return f"<a href='{href}'>{icon(icon_name)}<span>{label}</span></a>"


def icon(name: str) -> str:
    icons = {
        "dashboard": "<svg viewBox='0 0 24 24'><path d='M4 13h7V4H4v9Zm9 7h7V4h-7v16ZM4 20h7v-5H4v5Z'/></svg>",
        "counter": "<svg viewBox='0 0 24 24'><path d='M4 5h16v14H4V5Zm3 3v3h3V8H7Zm5 0v3h5V8h-5ZM7 13v3h3v-3H7Zm5 0v3h5v-3h-5Z'/></svg>",
        "shield": "<svg viewBox='0 0 24 24'><path d='M12 2 4 5v6c0 5 3.4 9.7 8 11 4.6-1.3 8-6 8-11V5l-8-3Zm-1 14-3-3 1.4-1.4 1.6 1.6 4.6-4.6L17 10l-6 6Z'/></svg>",
        "logout": "<svg viewBox='0 0 24 24'><path d='M10 17v-3H3v-4h7V7l5 5-5 5Zm1-15h9v20h-9v-2h7V4h-7V2Z'/></svg>",
        "user": "<svg viewBox='0 0 24 24'><path d='M12 12a5 5 0 1 0-5-5 5 5 0 0 0 5 5Zm0 2c-4.4 0-8 2.2-8 5v1h16v-1c0-2.8-3.6-5-8-5Z'/></svg>",
        "pulse": "<svg viewBox='0 0 24 24'><path d='M3 13h4l2-7 4 13 2-6h6v-2h-7.5l-.4 1.2L9 0 5.5 11H3v2Z'/></svg>",
        "users": "<svg viewBox='0 0 24 24'><path d='M16 11a4 4 0 1 0-4-4 4 4 0 0 0 4 4ZM8 12a4 4 0 1 0-4-4 4 4 0 0 0 4 4Zm8 2c-3 0-6 1.5-6 4v2h12v-2c0-2.5-3-4-6-4ZM8 14c-3.3 0-6 1.7-6 4v2h6v-2c0-1.5.7-2.8 2-3.8-.6-.1-1.3-.2-2-.2Z'/></svg>",
        "calendar": "<svg viewBox='0 0 24 24'><path d='M7 2h2v2h6V2h2v2h3v18H4V4h3V2Zm13 8H4v10h16V10Z'/></svg>",
        "send": "<svg viewBox='0 0 24 24'><path d='M2 21 23 12 2 3v7l15 2-15 2v7Z'/></svg>",
        "megaphone": "<svg viewBox='0 0 24 24'><path d='M3 10v4h3l5 4V6l-5 4H3Zm11.5-2.8V17a6 6 0 0 0 0-9.8ZM13 9.3v5.4a4 4 0 0 0 0-5.4Z'/></svg>",
        "search": "<svg viewBox='0 0 24 24'><path d='m21 19.6-5.2-5.2A7.5 7.5 0 1 0 14.4 16L19.6 21 21 19.6ZM4 10.5A5.5 5.5 0 1 1 9.5 16 5.5 5.5 0 0 1 4 10.5Z'/></svg>",
        "file-json": "<svg viewBox='0 0 24 24'><path d='M14 2H6v20h12V6l-4-4Zm-1 2.5L15.5 7H13V4.5ZM9 17H7v-2h2v2Zm0-4H7v-2h2v2Zm8 4h-2v-2h2v2Zm0-4h-2v-2h2v2Z'/></svg>",
        "save": "<svg viewBox='0 0 24 24'><path d='M17 3H4v18h16V6l-3-3ZM7 5h8v5H7V5Zm10 14H7v-6h10v6Z'/></svg>",
        "scan": "<svg viewBox='0 0 24 24'><path d='M4 4h6v2H6v4H4V4Zm10 0h6v6h-2V6h-4V4ZM6 14v4h4v2H4v-6h2Zm12 0h2v6h-6v-2h4v-4ZM8 11h8v2H8v-2Z'/></svg>",
        "spark": "<svg viewBox='0 0 24 24'><path d='m12 2 2.2 6.8H21l-5.5 4 2.1 6.8-5.6-4.2-5.6 4.2 2.1-6.8-5.5-4h6.8L12 2Z'/></svg>",
        "alert": "<svg viewBox='0 0 24 24'><path d='M1 21h22L12 2 1 21Zm12-3h-2v-2h2v2Zm0-4h-2v-4h2v4Z'/></svg>",
        "trash": "<svg viewBox='0 0 24 24'><path d='M7 21h10l1-13H6l1 13ZM9 4l1-2h4l1 2h5v2H4V4h5Z'/></svg>",
        "help": "<svg viewBox='0 0 24 24'><path d='M11 18h2v-2h-2v2Zm1-16a7 7 0 0 0-7 7h2a5 5 0 1 1 8.6 3.5c-1.7 1.6-2.6 2.4-2.6 4.5h-2c0-3 1.4-4.3 3.2-5.9A3 3 0 0 0 12 4Z'/></svg>",
        "tools": "<svg viewBox='0 0 24 24'><path d='m14.7 6.3 3 3L20 7l-3-3-2.3 2.3ZM13.3 7.7 4 17v3h3l9.3-9.3-3-3ZM20.7 13.3l-2-2a6 6 0 0 1-7.4 7.4l2 2A8 8 0 0 0 20.7 13.3ZM10.7 3.3 8.7 5.3A8 8 0 0 0 3.3 10.7l2 2a6 6 0 0 1 5.4-5.4Z'/></svg>",
        "refresh": "<svg viewBox='0 0 24 24'><path d='M12 5a7 7 0 0 1 6.2 3.8L16 11h6V5l-2.2 2.2A9 9 0 1 0 21 12h-2a7 7 0 1 1-7-7Z'/></svg>",
        "layers": "<svg viewBox='0 0 24 24'><path d='M12 2 1 8l11 6 9-4.9V17h2V8L12 2Zm0 14L4.2 11.8 1 13.5l11 6 11-6-3.2-1.7L12 16Z'/></svg>",
        "flask": "<svg viewBox='0 0 24 24'><path d='M9 2v2l1 1v4.6L4.4 18A3 3 0 0 0 7 22h10a3 3 0 0 0 2.6-4L14 9.6V5l1-1V2H9Zm2 8h2l5 8a1 1 0 0 1-.9 2H7a1 1 0 0 1-.9-2l5-8Z'/></svg>",
        "arrow-up": "<svg viewBox='0 0 24 24'><path d='m12 5 7 7-1.4 1.4-4.6-4.6V20h-2V8.8l-4.6 4.6L5 12l7-7Z'/></svg>",
    }
    return icons.get(name, icons["spark"])


def can(user: WebUser, permission: str) -> bool:
    return user.is_superuser or permission in user.permissions


def filter_metrics_for_user(metrics: dict, user: WebUser) -> dict:
    filtered = {"uptime_seconds": metrics["uptime_seconds"]}
    if can(user, "stats_overview"):
        filtered["users"] = metrics["users"]
        filtered["extra"] = metrics["extra"]
    if can(user, "stats_users"):
        filtered["user_rows"] = metrics["user_rows"]
    if can(user, "stats_services"):
        filtered["services"] = metrics["services"]
    if can(user, "stats_schedule"):
        filtered["schedule"] = metrics["schedule"]
    if can(user, "stats_delivery"):
        filtered["delivery"] = metrics["delivery"]
    if can(user, "stats_lesson_counters"):
        filtered["lesson_counters"] = metrics["lesson_counters"]
    return filtered


def html_escape(value: object) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def json_dumps(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2)


def parse_json_payload(value: str) -> dict:
    import json

    payload = json.loads(value)
    if isinstance(payload, list):
        return {"groups": payload}
    if not isinstance(payload, dict):
        raise ValueError("JSON должен быть объектом или списком групп.")
    return payload


def problems_html(problems: list[dict], saved: bool) -> str:
    if saved:
        return "<div class='alert good'>Проверено и сохранено.</div>"
    if not problems:
        return "<div class='alert good'>Ошибок не найдено. Можно сохранять.</div>"
    rows = "".join(f"<li class='{problem['level']}'>{html_escape(problem['message'])}</li>" for problem in problems)
    return f"<div class='alert'><b>Результат проверки</b><ul>{rows}</ul></div>"


STYLE = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&display=swap');
:root{--bg:#0b0d10;--surface:#14171c;--surface2:#1a1e25;--line:#2a3039;--text:#f1eadf;--muted:#a9a195;--accent:#d7c8aa;--accent2:#8f856f;--good:#7fba8d;--warn:#d0a85f;--bad:#c77878}
*{box-sizing:border-box} body{margin:0;font:14px/1.55 Montserrat,system-ui,sans-serif;background:var(--bg);color:var(--text)}
svg{width:18px;height:18px;fill:currentColor;flex:0 0 auto;display:block} a{color:inherit;text-decoration:none} h1,h2,h3,p{margin:0} h1{font-size:28px;font-weight:700;letter-spacing:0} h2{font-size:25px;font-weight:700;letter-spacing:0} h3{font-size:16px;font-weight:700;letter-spacing:0}
.shell{display:grid;grid-template-columns:280px 1fr;min-height:100vh}.sidebar{position:sticky;top:0;height:100vh;padding:22px;background:#101318;border-right:1px solid var(--line);display:flex;flex-direction:column;gap:24px}.brand{display:flex;gap:12px;align-items:center}.brand b{display:block;font-size:16px}.brand small{display:block;color:var(--muted);font-size:12px}.brand-mark{display:grid;place-items:center;width:40px;height:40px;border-radius:8px;background:#1d211f;border:1px solid #3b382f;color:var(--accent)}
nav{display:grid;gap:8px}.sidebar nav a{display:flex;gap:10px;align-items:center;padding:11px 12px;border-radius:8px;color:#d8d0c4;transition:.16s ease}.sidebar nav a:hover{background:#1a1e25;color:var(--text)}.logout{margin-top:auto}.workspace{min-width:0}.topbar{display:flex;justify-content:space-between;align-items:center;padding:26px 32px}.eyebrow{text-transform:uppercase;letter-spacing:.16em;color:var(--accent);font-size:11px;font-weight:800}.user-pill{display:flex;gap:8px;align-items:center;border:1px solid var(--line);background:#12161b;border-radius:999px;padding:9px 13px;color:#e4dccf}
main{max-width:1440px;margin:0 auto;padding:0 32px 42px}.hero,.page-head{position:relative;overflow:hidden;display:flex;justify-content:space-between;gap:24px;min-height:150px;padding:26px;border:1px solid var(--line);border-radius:8px;background:#12161b;box-shadow:0 18px 55px rgba(0,0,0,.22);margin-bottom:18px}.compact-head{min-height:auto}.hero p,.page-head p{max-width:760px;color:var(--muted);margin-top:10px}.to-top{position:fixed;right:22px;bottom:22px;z-index:20;box-shadow:0 12px 35px rgba(0,0,0,.35)}.icon-only{width:46px;height:46px;padding:0;border-radius:999px}.dashboard-block{margin-bottom:18px}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:18px;margin-bottom:18px;box-shadow:0 14px 45px rgba(0,0,0,.18);animation:rise .18s ease-out}.panel-title{display:flex;gap:10px;align-items:center;margin-bottom:14px}.icon{display:grid;place-items:center;width:34px;height:34px;border-radius:8px;color:var(--accent);background:#1b1e22;border:1px solid #343942;flex:0 0 auto}
@keyframes rise{from{opacity:.55;transform:translateY(5px)}to{opacity:1;transform:none}}.metric-grid,.service-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.metric-grid.compact{grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}.card,.service-card{position:relative;overflow:hidden;background:var(--surface2);border:1px solid var(--line);border-radius:8px;padding:16px;min-height:108px}.card span,.service-card span{color:var(--muted);display:block;font-size:12px;font-weight:600}.card b,.service-card b{font-size:26px;line-height:1.2;display:block;margin-top:8px;font-weight:800}.card small,.service-card small{color:var(--muted)}
.status{display:inline-flex;gap:7px;align-items:center}.dot{width:9px;height:9px;border-radius:999px;background:var(--bad)}.dot.ok{background:var(--good)}button{display:inline-flex;gap:8px;align-items:center;justify-content:center;background:#e7dcc9;color:#111418;border:0;border-radius:8px;padding:10px 14px;cursor:pointer;font-weight:700;transition:.16s ease}button:hover{background:#f1eadf}button.ghost,button.secondary{background:#1b1f26;color:var(--text);border:1px solid var(--line)}button.danger{color:#f0b7b7}
.login{display:grid;place-items:center;min-height:100vh}.narrow{width:min(390px,calc(100vw - 40px))}.login .panel{padding:28px}.login h1{margin-bottom:18px}label{display:grid;gap:7px;margin:0 0 12px;color:#ddd5c8;font-weight:600}input,select,textarea{width:100%;border:1px solid var(--line);border-radius:8px;padding:10px 11px;background:#0e1115;color:var(--text);font:inherit;outline:none}input:focus,select:focus,textarea:focus{border-color:#5a5346;box-shadow:0 0 0 3px rgba(215,200,170,.1)}textarea{min-height:620px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;line-height:1.45;resize:vertical}
.toolbar{display:grid;grid-template-columns:minmax(240px,1fr) 180px 190px;gap:12px;margin:4px 0 16px}.search{position:relative;margin:0}.search svg{position:absolute;left:11px;top:12px;color:var(--muted)}.search input{padding-left:38px}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:8px}.section-gap{margin-top:14px}table{width:100%;border-collapse:collapse;min-width:720px}td,th{padding:11px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:#cfc6b8;background:#101318;font-size:12px;text-transform:uppercase;letter-spacing:.08em}tr:hover td{background:#181c22}.profile-link{color:#eadfcf;text-decoration:underline;text-decoration-color:#5b5549;text-underline-offset:3px}
.editor-layout{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:18px}.editor-card,.guide-card{margin-bottom:0}.actions{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}.guide-list{display:grid;gap:12px;padding-left:18px;color:#d8d0c4}.muted{color:var(--muted)}.grid-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}.grid-form button,.checks{grid-column:1/-1}.checks{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px}.check{display:flex;gap:8px;align-items:center;margin:0;color:#ddd5c8;background:#101318;border:1px solid var(--line);border-radius:8px;padding:10px;min-height:46px}.check input{width:auto}.check span:first-of-type{flex:1}.check .help{margin-left:auto}
.help{position:relative;display:grid;place-items:center;width:24px;height:24px;color:var(--accent);cursor:help}.help svg{width:16px;height:16px}.tooltip{position:absolute;right:0;bottom:30px;width:min(320px,80vw);padding:12px;border:1px solid #484238;border-radius:8px;background:#0f1115;color:var(--text);box-shadow:0 18px 45px rgba(0,0,0,.4);font-size:12px;line-height:1.45;opacity:0;pointer-events:none;transform:translateY(4px);transition:.14s ease;z-index:30}.help:hover .tooltip,.help:focus .tooltip{opacity:1;transform:none}
.collapsible{padding:0}.collapse-title{display:flex;align-items:center;gap:10px;padding:18px;cursor:pointer;list-style:none;min-height:76px}.collapse-title::-webkit-details-marker{display:none}.collapse-title h3{flex:1}.collapse-hint{margin-left:auto;color:var(--muted);font-size:12px;font-weight:700}.collapsible[open] .collapse-title{border-bottom:1px solid var(--line);margin-bottom:16px}.collapsible[open]>*:not(summary){margin-left:18px;margin-right:18px}.collapsible[open]>*:last-child{margin-bottom:18px}.nested-details{margin-top:18px;border:1px solid var(--line);border-radius:8px;background:#101318}.nested-details .collapse-title{padding:14px;min-height:60px}.nested-details[open] .collapse-title{border-bottom:1px solid var(--line);margin-bottom:12px}.nested-details[open] .table-wrap{margin:0 12px 12px}.user-admin-list{display:grid;gap:14px}.admin-user-card{border:1px solid var(--line);border-radius:8px;background:#101318;padding:14px}.admin-user-form{display:grid;gap:14px}.admin-user-head{display:grid;grid-template-columns:minmax(220px,1fr) minmax(220px,320px);gap:14px;align-items:start}.password-inline{margin:0}
.two-col{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.action-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.action-card{display:grid;gap:10px;padding:16px;border:1px solid var(--line);border-radius:8px;background:#101318}.action-card b{display:flex;gap:8px;align-items:center}.action-card p{color:var(--muted)}.broadcast-form{display:grid;gap:12px}.broadcast-form textarea{min-height:180px}.editor-grid{display:grid;gap:12px}.editor-card-row{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:12px;align-items:center;padding:14px;border:1px solid var(--line);border-radius:8px;background:#101318}.profile-link.strong{font-weight:700}.chip-ok{border-color:#304a36;color:#bbf7d0}.summary-gap{margin-top:6px}
.head-chips,.chips{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.chip{display:inline-flex;align-items:center;min-height:28px;border:1px solid #3a372f;background:#1a1d20;color:#ded4c5;border-radius:999px;padding:5px 10px;font-size:12px;font-weight:700}.inline-form{display:grid;grid-template-columns:minmax(200px,1fr) 180px auto;gap:12px;align-items:end}.lesson-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:18px}.lesson-card{margin:0}.lesson-card-head{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;margin-bottom:14px}.subject-list{display:grid;gap:10px}.subject-row{border:1px solid var(--line);border-radius:8px;background:#101318;padding:0}.subject-row summary{display:flex;justify-content:space-between;gap:12px;align-items:center;cursor:pointer;padding:12px}.subject-row summary small{display:block;color:var(--muted);margin-top:3px}.subject-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:14px}.subject-form.edit{padding:0 12px 12px;margin-top:0}.subject-form button{grid-column:1/-1}.delete-line{padding:0 12px 12px}.empty-state{border:1px dashed #3a3f48;border-radius:8px;padding:18px;color:var(--muted);background:#101318}.empty-state.small{padding:12px}.json-details summary{display:flex;gap:10px;align-items:center;cursor:pointer;font-weight:800}.json-form{margin-top:14px}
.alert{padding:13px;border-radius:8px;background:#241d0d;border:1px solid #5b4315;margin-bottom:12px;color:#fde68a}.alert.good{background:#0f241d;border-color:#1f6b4b;color:#bbf7d0}.alert.bad,.error{color:#fecdd3;background:#2a1119;border-color:#7f1d1d}.warning{color:#fbbf24}.ok{color:var(--good)}.bad{color:var(--bad)}
@media (max-width:900px){.shell{grid-template-columns:1fr}.sidebar{position:relative;height:auto}.topbar{padding:20px}main{padding:0 16px 32px}.toolbar,.editor-layout,.inline-form,.subject-form,.admin-user-head,.two-col,.action-grid,.editor-card-row{grid-template-columns:1fr}.lesson-grid{grid-template-columns:1fr}.sidebar nav{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}.to-top{right:14px;bottom:14px}}
</style>
"""

DASHBOARD_SCRIPT = """
<script>
let metrics=null;
const fmt=n=>Number(n||0).toLocaleString('ru-RU');
const uptime=s=>{s=Number(s||0);const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return `${h}ч ${m}м`};
function card(name,value,extra=''){return `<div class="card"><span>${name}</span><b>${value}</b><small>${extra}</small></div>`}
function esc(v){return String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]))}
function profileUrl(u){
  if(u.platform==='vk') return `https://vk.com/id${u.user_id}`;
  if(u.username) return `https://t.me/${String(u.username).replace(/^@/,'')}`;
  return `tg://user?id=${u.user_id}`;
}
function serviceCard(name,v){return `<div class="service-card"><span>${name}</span><b class="status"><i class="dot ${v.ok?'ok':''}"></i>${v.ok?'OK':'Недоступен'}</b></div>`}
function renderUsers(rows){
  const q=(document.querySelector('#userSearch')?.value||'').toLowerCase();
  const p=document.querySelector('#platformFilter')?.value||'', k=document.querySelector('#kindFilter')?.value||'';
  let filtered=rows.filter(u=>(!p||u.platform===p)&&(!q||JSON.stringify(u).toLowerCase().includes(q)));
  if(k==='teacher') filtered=filtered.filter(u=>u.subscription_type==='teacher');
  if(k==='group') filtered=filtered.filter(u=>u.subscription_type==='group');
  if(k==='new') filtered=filtered.filter(u=>u.is_new);
  if(k==='old') filtered=filtered.filter(u=>!u.is_new);
  const table=document.querySelector('#usersTable'); if(!table) return;
  table.innerHTML='<tr><th>Платформа</th><th>ID</th><th>Имя</th><th>Подписка</th><th>Создан</th><th>Последний визит</th></tr>'+filtered.map(u=>`<tr><td><b>${u.platform==='telegram'?'TG':'VK'}</b></td><td><a class="profile-link" href="${profileUrl(u)}" target="_blank" rel="noreferrer">${u.user_id}</a></td><td><a class="profile-link" href="${profileUrl(u)}" target="_blank" rel="noreferrer">${esc(u.full_name||u.username||'профиль')}</a></td><td>${esc(u.subscription_title||'-')}<br><small>${esc(u.subscription_type||'')}</small></td><td>${esc(u.created_at)}</td><td>${esc(u.last_seen_at)}</td></tr>`).join('');
}
async function load(){
  const r=await fetch('/api/metrics'); metrics=await r.json();
  const o=document.querySelector('#overview'); if(o) o.innerHTML=[
    card('Аптайм',uptime(metrics.uptime_seconds),'с момента запуска'),card('Юзеров',fmt(metrics.users.total),`TG ${fmt(metrics.users.telegram)} / VK ${fmt(metrics.users.vk)}`),card('Новых за 7 дней',fmt(metrics.users.new_7d),`старых ${fmt(metrics.users.old)}`),card('Подписок на преподов',fmt(metrics.users.teachers),`на группы ${fmt(metrics.users.groups)}`),card('Тихих 30+ дней',fmt(metrics.extra.quiet_users),'без активности')
  ].join('');
  const s=document.querySelector('#services'); if(s) s.innerHTML=Object.entries(metrics.services).map(([k,v])=>serviceCard(k, v)).join('');
  renderUsers(metrics.user_rows||[]);
  const sch=document.querySelector('#schedule'); if(sch){sch.innerHTML=`<div class="metric-grid compact">${card('Последний парс',esc(metrics.schedule.latest_parse?.created_at||'-'),esc(metrics.schedule.latest_parse?.source_title||'-'))}${card('Последнее изменение',esc(metrics.schedule.latest_change?.created_at||'-'),esc(metrics.schedule.latest_change?.source_title||'-'))}${card('Активных групп',fmt(metrics.schedule.active_groups_total),'с подписчиками')}</div><div class="table-wrap section-gap"><table><tr><th>Группа</th><th>Юзеров</th></tr>${metrics.schedule.active_groups.map(g=>`<tr><td>${esc(g.subscription_title)}</td><td>${g.users_count}</td></tr>`).join('')}</table></div><details class="nested-details"><summary class="collapse-title"><h3>Последние изменения</h3><span class="collapse-hint">Развернуть</span></summary><div class="table-wrap"><table><tr><th>Когда</th><th>Источник</th><th>Сообщение</th></tr>${metrics.schedule.changes.map(c=>`<tr><td>${esc(c.created_at)}</td><td>${esc(c.source_title||'')}</td><td>${esc((c.message||'').slice(0,240))}</td></tr>`).join('')}</table></div></details>`}
  const d=document.querySelector('#delivery'); if(d){const t=metrics.delivery.today,a=metrics.delivery.total;d.innerHTML=[card('Сегодня доставлено',fmt(t.sent),`Rabbit ${fmt(t.via_broker)} / direct ${fmt(t.direct)}`),card('Сегодня TG/VK',`${fmt(t.telegram)} / ${fmt(t.vk)}`,`ошибок ${fmt(t.failed)}`),card('Всего доставлено',fmt(a.sent),`Rabbit ${fmt(a.via_broker)} / direct ${fmt(a.direct)}`),card('Всего TG/VK',`${fmt(a.telegram)} / ${fmt(a.vk)}`,`ошибок ${fmt(a.failed)}`)].join('')}
  const l=document.querySelector('#lessonsStats'); if(l){const lc=metrics.lesson_counters;l.innerHTML=`<div class="metric-grid compact">${card('Счетчиков',fmt(lc.configured),`групп ${fmt(lc.groups)}`)}${card('Учтено сегодня',fmt(lc.counted_today),'после ночной проверки')}${card('Последний учет',lc.last_event?.created_at||'-',lc.last_event?.subject||'')}</div><div class="table-wrap" style="margin-top:14px"><table><tr><th>Группа</th><th>Предмет</th><th>Преподаватель</th><th>Прогресс</th></tr>${lc.counters.map(c=>`<tr><td>${c.schedule_id}</td><td>${c.subject}</td><td>${c.teacher}</td><td><b>${c.passed_count}/${c.total_count}</b></td></tr>`).join('')}</table></div>`}
}
document.addEventListener('input',e=>{if(['userSearch','platformFilter','kindFilter'].includes(e.target.id)&&metrics)renderUsers(metrics.user_rows||[])});
load(); setInterval(load,30000);
</script>
"""
