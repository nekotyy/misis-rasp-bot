from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.config import Settings
from src.db import Database
from src.group_catalog import GroupCatalog
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
        """
        <main class="login">
          <form method="post" action="/login" class="panel narrow">
            <h1>Вход</h1>
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
    content = f"""
    <section class="page-head">
      <div>
        <p class="eyebrow">Конфигурация семестра</p>
        <h2>Счетчики пройденных пар</h2>
        <p>Правь JSON, проверяй дисциплины по расписанию группы и сохраняй только валидный список.</p>
      </div>
    </section>
    <section class="editor-layout">
      <form method="post" class="panel editor-card">
        <div class="panel-title"><span class="icon">{icon("file-json")}</span><h3>lesson_counters.json</h3></div>
        <textarea name="payload" spellcheck="false">{html_escape(json_dumps(payload))}</textarea>
        <div class="actions">
          <button class="secondary" name="mode" value="validate" type="submit">{icon("scan")} Проверить</button>
          <button name="mode" value="save" type="submit">{icon("save")} Проверить и сохранить</button>
        </div>
      </form>
      <aside class="panel guide-card">
        <div class="panel-title"><span class="icon">{icon("spark")}</span><h3>Как это работает</h3></div>
        <ul class="guide-list">
          <li>Можно указать <b>schedule_id</b> или <b>group_name</b>.</li>
          <li>Название дисциплины нормализуется: регистр, точки и дефисы не мешают.</li>
          <li>Если предмета нет в расписании группы, вебка не даст сохранить.</li>
          <li>Счетчик <b>passed</b> не сбрасывается при рестарте, пока запись остается в JSON.</li>
        </ul>
      </aside>
    </section>
    """
    return layout("Счетчики пар", content, user)


@app.post("/lessons", response_class=HTMLResponse)
async def save_lessons(
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

    content = f"""
    <section class="page-head">
      <div>
        <p class="eyebrow">Конфигурация семестра</p>
        <h2>Счетчики пройденных пар</h2>
        <p>Результат проверки ниже. Ошибки блокируют сохранение, предупреждения оставляют решение за тобой.</p>
      </div>
    </section>
    <section class="editor-layout">
      <form method="post" class="panel editor-card">
        <div class="panel-title"><span class="icon">{icon("file-json")}</span><h3>lesson_counters.json</h3></div>
      {report}
        <textarea name="payload" spellcheck="false">{html_escape(editor_value)}</textarea>
        <div class="actions">
          <button class="secondary" name="mode" value="validate" type="submit">{icon("scan")} Проверить</button>
          <button name="mode" value="save" type="submit">{icon("save")} Проверить и сохранить</button>
        </div>
      </form>
      <aside class="panel guide-card">
        <div class="panel-title"><span class="icon">{icon("alert")}</span><h3>Валидация</h3></div>
        <p class="muted">Проверка ходит на сайт расписания, поэтому может занять несколько секунд.</p>
      </aside>
    </section>
    """
    return layout("Счетчики пар", content, user)


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


def dashboard_html(user: WebUser) -> str:
    cards = []
    if can(user, "stats_overview"):
        cards.append(
            """
            <section class="hero">
              <div>
                <p class="eyebrow">Live monitor</p>
                <h2>Пульс бота расписания</h2>
                <p>Обновляется каждые 30 секунд. Смотри нагрузку, доставку, подписки и состояние счетчиков в одном месте.</p>
              </div>
              <div class="hero-orbit">
                <span></span><span></span><span></span>
              </div>
            </section>
            <section><div id="overview" class="metric-grid"></div></section>
            """
        )
    if can(user, "stats_services"):
        cards.append(f"<section class='panel'><div class='panel-title'><span class='icon'>{icon('pulse')}</span><h3>Сервисы</h3></div><div id='services' class='service-grid'></div></section>")
    if can(user, "stats_users"):
        cards.append(
            f"""
            <section class="panel">
              <div class="panel-title"><span class="icon">{icon('users')}</span><h3>Пользователи</h3></div>
              <div class="toolbar">
                <label class="search">{icon('search')}<input id="userSearch" placeholder="Имя, ID, группа, преподаватель"></label>
                <select id="platformFilter"><option value="">Все платформы</option><option value="telegram">Telegram</option><option value="vk">VK</option></select>
                <select id="kindFilter"><option value="">Все подписки</option><option value="teacher">Преподаватели</option><option value="group">Группы</option><option value="new">Новые</option><option value="old">Старые</option></select>
              </div>
              <div class="table-wrap"><table id="usersTable"></table></div>
            </section>
            """
        )
    if can(user, "stats_schedule"):
        cards.append(f"<section class='panel'><div class='panel-title'><span class='icon'>{icon('calendar')}</span><h3>Расписание и изменения</h3></div><div id='schedule'></div></section>")
    if can(user, "stats_delivery"):
        cards.append(f"<section class='panel'><div class='panel-title'><span class='icon'>{icon('send')}</span><h3>Доставка сообщений</h3></div><div id='delivery' class='metric-grid compact'></div></section>")
    if can(user, "stats_lesson_counters"):
        cards.append(f"<section class='panel'><div class='panel-title'><span class='icon'>{icon('counter')}</span><h3>Подсчет пар</h3></div><div id='lessonsStats'></div></section>")
    return "\n".join(cards) + DASHBOARD_SCRIPT


def web_users_html() -> str:
    permissions = "\n".join(
        f"<label class='check'><input type='checkbox' name='permissions' value='{permission}'> {label}</label>"
        for permission, label in PERMISSION_LABELS.items()
    )
    rows = "\n".join(
        f"<tr><td>{html_escape(user['login'])}</td><td>{'суперюзер' if user['is_superuser'] else ', '.join(user['permissions'])}</td><td>{'' if user['is_superuser'] else delete_form(user['login'])}</td></tr>"
        for user in auth_store.list_users()
    )
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
    <section class="panel"><div class="panel-title"><span class="icon">{icon('users')}</span><h3>Пользователи</h3></div><table><tr><th>Логин</th><th>Права</th><th></th></tr>{rows}</table></section>
    """


def delete_form(login: str) -> str:
    return f"<form method='post' action='/web-users/delete'><input type='hidden' name='login' value='{html_escape(login)}'><button class='ghost danger'>{icon('trash')} Удалить</button></form>"


def layout(title: str, content: str, user: WebUser) -> str:
    nav_items = [nav_link("/", "Метрики", "dashboard")]
    if can(user, "config_lesson_counters"):
        nav_items.append(nav_link("/lessons", "Счетчики пар", "counter"))
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
        "search": "<svg viewBox='0 0 24 24'><path d='m21 19.6-5.2-5.2A7.5 7.5 0 1 0 14.4 16L19.6 21 21 19.6ZM4 10.5A5.5 5.5 0 1 1 9.5 16 5.5 5.5 0 0 1 4 10.5Z'/></svg>",
        "file-json": "<svg viewBox='0 0 24 24'><path d='M14 2H6v20h12V6l-4-4Zm-1 2.5L15.5 7H13V4.5ZM9 17H7v-2h2v2Zm0-4H7v-2h2v2Zm8 4h-2v-2h2v2Zm0-4h-2v-2h2v2Z'/></svg>",
        "save": "<svg viewBox='0 0 24 24'><path d='M17 3H4v18h16V6l-3-3ZM7 5h8v5H7V5Zm10 14H7v-6h10v6Z'/></svg>",
        "scan": "<svg viewBox='0 0 24 24'><path d='M4 4h6v2H6v4H4V4Zm10 0h6v6h-2V6h-4V4ZM6 14v4h4v2H4v-6h2Zm12 0h2v6h-6v-2h4v-4ZM8 11h8v2H8v-2Z'/></svg>",
        "spark": "<svg viewBox='0 0 24 24'><path d='m12 2 2.2 6.8H21l-5.5 4 2.1 6.8-5.6-4.2-5.6 4.2 2.1-6.8-5.5-4h6.8L12 2Z'/></svg>",
        "alert": "<svg viewBox='0 0 24 24'><path d='M1 21h22L12 2 1 21Zm12-3h-2v-2h2v2Zm0-4h-2v-4h2v4Z'/></svg>",
        "trash": "<svg viewBox='0 0 24 24'><path d='M7 21h10l1-13H6l1 13ZM9 4l1-2h4l1 2h5v2H4V4h5Z'/></svg>",
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
:root{--bg:#090d14;--surface:#111824;--surface2:#151f2e;--line:#263244;--text:#eef4ff;--muted:#91a0b5;--accent:#6ee7f9;--accent2:#8b5cf6;--good:#34d399;--warn:#f59e0b;--bad:#fb7185}
*{box-sizing:border-box} body{margin:0;font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:radial-gradient(circle at 20% 0%,rgba(110,231,249,.16),transparent 28%),radial-gradient(circle at 90% 10%,rgba(139,92,246,.16),transparent 30%),var(--bg);color:var(--text)}
svg{width:18px;height:18px;fill:currentColor;flex:0 0 auto} a{color:inherit;text-decoration:none} h1,h2,h3,p{margin:0} h1{font-size:30px;letter-spacing:0} h2{font-size:26px;letter-spacing:0} h3{font-size:16px;letter-spacing:0}
.shell{display:grid;grid-template-columns:280px 1fr;min-height:100vh}.sidebar{position:sticky;top:0;height:100vh;padding:22px;background:rgba(12,18,28,.88);border-right:1px solid var(--line);backdrop-filter:blur(18px);display:flex;flex-direction:column;gap:24px}.brand{display:flex;gap:12px;align-items:center}.brand b{display:block;font-size:17px}.brand small{display:block;color:var(--muted)}.brand-mark{display:grid;place-items:center;width:42px;height:42px;border-radius:12px;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#08101b}
nav{display:grid;gap:8px}.sidebar nav a{display:flex;gap:10px;align-items:center;padding:11px 12px;border-radius:8px;color:#cbd6e6;transition:.18s ease}.sidebar nav a:hover{background:#172235;color:#fff;transform:translateX(3px)}.logout{margin-top:auto}.workspace{min-width:0}.topbar{display:flex;justify-content:space-between;align-items:center;padding:26px 32px}.eyebrow{text-transform:uppercase;letter-spacing:.16em;color:var(--accent);font-size:11px;font-weight:800}.user-pill{display:flex;gap:8px;align-items:center;border:1px solid var(--line);background:rgba(17,24,36,.72);border-radius:999px;padding:9px 13px;color:#d7e2f3}
main{max-width:1440px;margin:0 auto;padding:0 32px 42px}.hero,.page-head{position:relative;overflow:hidden;display:flex;justify-content:space-between;gap:24px;min-height:190px;padding:28px;border:1px solid var(--line);border-radius:8px;background:linear-gradient(135deg,rgba(21,31,46,.96),rgba(13,19,30,.92));box-shadow:0 24px 80px rgba(0,0,0,.32);margin-bottom:18px}.hero p,.page-head p{max-width:680px;color:var(--muted);margin-top:10px}.hero-orbit{position:relative;width:180px;min-width:180px}.hero-orbit span{position:absolute;border-radius:999px;border:1px solid rgba(110,231,249,.45);animation:float 5s ease-in-out infinite}.hero-orbit span:nth-child(1){inset:20px}.hero-orbit span:nth-child(2){inset:48px;animation-delay:.5s;border-color:rgba(139,92,246,.55)}.hero-orbit span:nth-child(3){inset:75px;background:linear-gradient(135deg,var(--accent),var(--accent2));border:0}
@keyframes float{50%{transform:translateY(-8px) scale(1.02)}}.panel{background:rgba(17,24,36,.82);border:1px solid var(--line);border-radius:8px;padding:18px;margin-bottom:18px;box-shadow:0 14px 50px rgba(0,0,0,.24);animation:rise .22s ease-out}.panel-title{display:flex;gap:10px;align-items:center;margin-bottom:14px}.icon{display:grid;place-items:center;width:34px;height:34px;border-radius:8px;color:var(--accent);background:#0d2530;border:1px solid #1f4250}
@keyframes rise{from{opacity:.4;transform:translateY(8px)}to{opacity:1;transform:none}}.metric-grid,.service-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.metric-grid.compact{grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}.card,.service-card{position:relative;overflow:hidden;background:linear-gradient(180deg,rgba(24,35,52,.95),rgba(17,24,36,.95));border:1px solid var(--line);border-radius:8px;padding:16px;min-height:108px}.card:before,.service-card:before{content:"";position:absolute;inset:0 0 auto;height:3px;background:linear-gradient(90deg,var(--accent),var(--accent2));opacity:.9}.card span,.service-card span{color:var(--muted);display:block}.card b,.service-card b{font-size:28px;line-height:1.2;display:block;margin-top:8px}.card small,.service-card small{color:var(--muted)}
.status{display:inline-flex;gap:7px;align-items:center}.dot{width:9px;height:9px;border-radius:999px;background:var(--bad);box-shadow:0 0 14px currentColor}.dot.ok{background:var(--good)}button{display:inline-flex;gap:8px;align-items:center;justify-content:center;background:linear-gradient(135deg,#0891b2,#7c3aed);color:white;border:0;border-radius:8px;padding:10px 14px;cursor:pointer;font-weight:700;transition:.16s ease}button:hover{transform:translateY(-1px);filter:brightness(1.08)}button.ghost,button.secondary{background:#1b2738;color:#dce8f8;border:1px solid var(--line)}button.danger{color:#fecdd3}
.login{display:grid;place-items:center;min-height:100vh}.narrow{width:min(390px,calc(100vw - 40px))}.login .panel{padding:28px}.login h1{margin-bottom:18px}label{display:grid;gap:7px;margin:0 0 12px;color:#dbe7f7}input,select,textarea{width:100%;border:1px solid var(--line);border-radius:8px;padding:10px 11px;background:#0c121c;color:var(--text);font:inherit;outline:none}input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(110,231,249,.12)}textarea{min-height:620px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;line-height:1.45;resize:vertical}
.toolbar{display:grid;grid-template-columns:minmax(240px,1fr) 180px 190px;gap:10px;margin-bottom:14px}.search{position:relative;margin:0}.search svg{position:absolute;left:11px;top:12px;color:var(--muted)}.search input{padding-left:38px}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:8px}table{width:100%;border-collapse:collapse;min-width:720px}td,th{padding:11px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:#a9b8cc;background:#101827;font-size:12px;text-transform:uppercase;letter-spacing:.08em}tr:hover td{background:#131d2b}
.editor-layout{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:18px}.editor-card,.guide-card{margin-bottom:0}.actions{display:flex;gap:10px;margin-top:12px}.guide-list{display:grid;gap:12px;padding-left:18px;color:#c9d6e7}.muted{color:var(--muted)}.grid-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}.grid-form button,.checks{grid-column:1/-1}.checks{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px}.check{display:flex;gap:8px;align-items:center;margin:0;color:#d7e2f3}.check input{width:auto}
.alert{padding:13px;border-radius:8px;background:#241d0d;border:1px solid #5b4315;margin-bottom:12px;color:#fde68a}.alert.good{background:#0f241d;border-color:#1f6b4b;color:#bbf7d0}.alert.bad,.error{color:#fecdd3;background:#2a1119;border-color:#7f1d1d}.warning{color:#fbbf24}.ok{color:var(--good)}.bad{color:var(--bad)}
@media (max-width:900px){.shell{grid-template-columns:1fr}.sidebar{position:relative;height:auto}.topbar{padding:20px}.hero-orbit{display:none}main{padding:0 16px 32px}.toolbar,.editor-layout{grid-template-columns:1fr}.sidebar nav{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}}
</style>
"""

DASHBOARD_SCRIPT = """
<script>
let metrics=null;
const fmt=n=>Number(n||0).toLocaleString('ru-RU');
const uptime=s=>{s=Number(s||0);const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return `${h}ч ${m}м`};
function card(name,value,extra=''){return `<div class="card"><span>${name}</span><b>${value}</b><small>${extra}</small></div>`}
function serviceCard(name,v){return `<div class="service-card"><span>${name}</span><b class="status"><i class="dot ${v.ok?'ok':''}"></i>${v.ok?'OK':'Сбой'}</b><small>${v.label||''}</small></div>`}
function renderUsers(rows){
  const q=(document.querySelector('#userSearch')?.value||'').toLowerCase();
  const p=document.querySelector('#platformFilter')?.value||'', k=document.querySelector('#kindFilter')?.value||'';
  let filtered=rows.filter(u=>(!p||u.platform===p)&&(!q||JSON.stringify(u).toLowerCase().includes(q)));
  if(k==='teacher') filtered=filtered.filter(u=>u.subscription_type==='teacher');
  if(k==='group') filtered=filtered.filter(u=>u.subscription_type==='group');
  if(k==='new') filtered=filtered.filter(u=>u.is_new);
  if(k==='old') filtered=filtered.filter(u=>!u.is_new);
  const table=document.querySelector('#usersTable'); if(!table) return;
  table.innerHTML='<tr><th>Платформа</th><th>ID</th><th>Имя</th><th>Подписка</th><th>Создан</th><th>Последний визит</th></tr>'+filtered.map(u=>`<tr><td><b>${u.platform==='telegram'?'TG':'VK'}</b></td><td>${u.user_id}</td><td>${u.full_name||u.username||''}</td><td>${u.subscription_title||'-'}<br><small>${u.subscription_type||''}</small></td><td>${u.created_at}</td><td>${u.last_seen_at}</td></tr>`).join('');
}
async function load(){
  const r=await fetch('/api/metrics'); metrics=await r.json();
  const o=document.querySelector('#overview'); if(o) o.innerHTML=[
    card('Аптайм',uptime(metrics.uptime_seconds),'текущая сессия вебки'),card('Юзеров',fmt(metrics.users.total),`TG ${fmt(metrics.users.telegram)} / VK ${fmt(metrics.users.vk)}`),card('Новых за 7 дней',fmt(metrics.users.new_7d),`старых ${fmt(metrics.users.old)}`),card('Подписок на преподов',fmt(metrics.users.teachers),`на группы ${fmt(metrics.users.groups)}`),card('Тихих 30+ дней',fmt(metrics.extra.quiet_users),'моя доп. метрика')
  ].join('');
  const s=document.querySelector('#services'); if(s) s.innerHTML=Object.entries(metrics.services).map(([k,v])=>serviceCard(k, v)).join('');
  renderUsers(metrics.user_rows||[]);
  const sch=document.querySelector('#schedule'); if(sch){sch.innerHTML=`<div class="metric-grid compact">${card('Последний парс',metrics.schedule.latest_parse?.created_at||'-',metrics.schedule.latest_parse?.source_title||'-')}${card('Последнее изменение',metrics.schedule.latest_change?.created_at||'-',metrics.schedule.latest_change?.source_title||'-')}${card('Активных групп',fmt(metrics.schedule.active_groups_total),'с подписчиками')}</div><div class="table-wrap" style="margin-top:14px"><table><tr><th>Группа</th><th>Юзеров</th></tr>${metrics.schedule.active_groups.map(g=>`<tr><td>${g.subscription_title}</td><td>${g.users_count}</td></tr>`).join('')}</table></div><div class="panel-title" style="margin-top:18px"><h3>Последние изменения</h3></div><div class="table-wrap"><table><tr><th>Когда</th><th>Источник</th><th>Сообщение</th></tr>${metrics.schedule.changes.map(c=>`<tr><td>${c.created_at}</td><td>${c.source_title||''}</td><td>${(c.message||'').slice(0,240)}</td></tr>`).join('')}</table></div>`}
  const d=document.querySelector('#delivery'); if(d){const t=metrics.delivery.today,a=metrics.delivery.total;d.innerHTML=[card('Сегодня доставлено',fmt(t.sent),`Rabbit ${fmt(t.via_broker)} / direct ${fmt(t.direct)}`),card('Сегодня TG/VK',`${fmt(t.telegram)} / ${fmt(t.vk)}`,`ошибок ${fmt(t.failed)}`),card('Всего доставлено',fmt(a.sent),`Rabbit ${fmt(a.via_broker)} / direct ${fmt(a.direct)}`),card('Всего TG/VK',`${fmt(a.telegram)} / ${fmt(a.vk)}`,`ошибок ${fmt(a.failed)}`)].join('')}
  const l=document.querySelector('#lessonsStats'); if(l){const lc=metrics.lesson_counters;l.innerHTML=`<div class="metric-grid compact">${card('Счетчиков',fmt(lc.configured),`групп ${fmt(lc.groups)}`)}${card('Учтено сегодня',fmt(lc.counted_today),'после ночной проверки')}${card('Последний учет',lc.last_event?.created_at||'-',lc.last_event?.subject||'')}</div><div class="table-wrap" style="margin-top:14px"><table><tr><th>Группа</th><th>Предмет</th><th>Преподаватель</th><th>Прогресс</th></tr>${lc.counters.map(c=>`<tr><td>${c.schedule_id}</td><td>${c.subject}</td><td>${c.teacher}</td><td><b>${c.passed_count}/${c.total_count}</b></td></tr>`).join('')}</table></div>`}
}
document.addEventListener('input',e=>{if(['userSearch','platformFilter','kindFilter'].includes(e.target.id)&&metrics)renderUsers(metrics.user_rows||[])});
load(); setInterval(load,30000);
</script>
"""
