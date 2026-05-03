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
from web_configurator.env_config import EDITABLE_ENV_KEYS, read_env_values, write_env_values
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
            <label>Логин <input name="login" autocomplete="username" required></label>
            <label>Пароль <input name="password" type="password" autocomplete="current-password" required></label>
            <button type="submit">Войти</button>
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


@app.get("/config", response_class=HTMLResponse)
async def config_page(user: Annotated[WebUser, Depends(require("config_bot"))]) -> str:
    values = read_env_values(ENV_PATH)
    fields = "\n".join(
        f'<label>{key}<input name="{key}" value="{html_escape(values.get(key, ""))}"></label>'
        for key in EDITABLE_ENV_KEYS
    )
    return layout("Конфиг", f"<form method='post' class='panel grid-form'>{fields}<button>Сохранить</button></form>", user)


@app.post("/config")
async def save_config(request: Request, _: Annotated[WebUser, Depends(require("config_bot"))]) -> RedirectResponse:
    form = await request.form()
    write_env_values(ENV_PATH, {key: str(form.get(key, "")) for key in EDITABLE_ENV_KEYS})
    return RedirectResponse("/config?saved=1", status_code=303)


@app.get("/lessons", response_class=HTMLResponse)
async def lessons_page(user: Annotated[WebUser, Depends(require("config_lesson_counters"))]) -> str:
    payload = load_lesson_config(Settings.from_env().lesson_counters_path)
    content = f"""
    <section class="panel">
      <h2>Счетчики пар</h2>
      <form method="post">
        <textarea name="payload" spellcheck="false">{html_escape(json_dumps(payload))}</textarea>
        <div class="actions">
          <button name="mode" value="validate">Проверить</button>
          <button name="mode" value="save">Проверить и сохранить</button>
        </div>
      </form>
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
    <section class="panel">
      <h2>Счетчики пар</h2>
      {report}
      <form method="post">
        <textarea name="payload" spellcheck="false">{html_escape(editor_value)}</textarea>
        <div class="actions">
          <button name="mode" value="validate">Проверить</button>
          <button name="mode" value="save">Проверить и сохранить</button>
        </div>
      </form>
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
        cards.append("<section class='panel'><h2>Обзор</h2><div id='overview' class='cards'></div></section>")
    if can(user, "stats_services"):
        cards.append("<section class='panel'><h2>Сервисы</h2><div id='services' class='cards'></div></section>")
    if can(user, "stats_users"):
        cards.append("<section class='panel'><h2>Пользователи</h2><div class='filters'><input id='userSearch' placeholder='Поиск'><select id='platformFilter'><option value=''>TG/VK</option><option value='telegram'>TG</option><option value='vk'>VK</option></select><select id='kindFilter'><option value=''>Все</option><option value='teacher'>Преподы</option><option value='group'>Группы</option><option value='new'>Новые</option><option value='old'>Старые</option></select></div><div class='table-wrap'><table id='usersTable'></table></div></section>")
    if can(user, "stats_schedule"):
        cards.append("<section class='panel'><h2>Расписание</h2><div id='schedule'></div></section>")
    if can(user, "stats_delivery"):
        cards.append("<section class='panel'><h2>Доставка</h2><div id='delivery' class='cards'></div></section>")
    if can(user, "stats_lesson_counters"):
        cards.append("<section class='panel'><h2>Подсчет пар</h2><div id='lessonsStats'></div></section>")
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
      <h2>Создать / обновить пользователя</h2>
      <form method="post" class="grid-form">
        <label>Логин <input name="login" required></label>
        <label>Пароль <input name="password" type="password" placeholder="оставь пустым, чтобы не менять"></label>
        <div class="checks">{permissions}</div>
        <button>Сохранить</button>
      </form>
    </section>
    <section class="panel"><h2>Пользователи</h2><table><tr><th>Логин</th><th>Права</th><th></th></tr>{rows}</table></section>
    """


def delete_form(login: str) -> str:
    return f"<form method='post' action='/web-users/delete'><input type='hidden' name='login' value='{html_escape(login)}'><button class='ghost'>Удалить</button></form>"


def layout(title: str, content: str, user: WebUser) -> str:
    nav_items = ["<a href='/'>Метрики</a>"]
    if can(user, "config_bot"):
        nav_items.append("<a href='/config'>Конфиг</a>")
    if can(user, "config_lesson_counters"):
        nav_items.append("<a href='/lessons'>Счетчики пар</a>")
    if can(user, "manage_web_users"):
        nav_items.append("<a href='/web-users'>Веб-пользователи</a>")
    nav = "".join(nav_items)
    return base_page(
        title,
        f"""
        <header><h1>{title}</h1><nav>{nav}<form method="post" action="/logout"><button class="ghost">Выйти</button></form></nav></header>
        <main>{content}</main>
        """,
    )


def base_page(title: str, body: str) -> str:
    return f"<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html_escape(title)}</title>{STYLE}</head><body>{body}</body></html>"


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
body{margin:0;font:14px/1.45 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f6f7f9;color:#18202a}
header{display:flex;gap:24px;align-items:center;justify-content:space-between;padding:18px 28px;background:#fff;border-bottom:1px solid #dfe3e8;position:sticky;top:0}
h1{font-size:22px;margin:0} h2{font-size:18px;margin:0 0 14px} main{max-width:1280px;margin:24px auto;padding:0 20px}
nav{display:flex;gap:12px;align-items:center} a{color:#155eef;text-decoration:none} button{background:#155eef;color:white;border:0;border-radius:6px;padding:9px 13px;cursor:pointer}
button.ghost{background:#eef2f7;color:#202936} .panel{background:#fff;border:1px solid #dfe3e8;border-radius:8px;padding:18px;margin-bottom:18px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.card{background:#f8fafc;border:1px solid #e5e9ef;border-radius:8px;padding:14px}.card b{font-size:22px;display:block}
.login{display:grid;place-items:center;min-height:100vh}.narrow{width:min(360px,calc(100vw - 40px))} label{display:grid;gap:5px;margin:0 0 12px} input,select,textarea{border:1px solid #ccd3dc;border-radius:6px;padding:9px;background:#fff;font:inherit}textarea{width:100%;min-height:520px;font-family:ui-monospace,Consolas,monospace;box-sizing:border-box}
.grid-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}.grid-form button,.actions{grid-column:1/-1}.checks{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:4px;grid-column:1/-1}.check{display:flex;gap:8px;align-items:center;margin:0}
.filters{display:flex;gap:10px;margin-bottom:12px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #e8ecf1;text-align:left;vertical-align:top}
.alert{padding:12px;border-radius:8px;background:#fff7e0;border:1px solid #f0d48a;margin-bottom:12px}.alert.good{background:#eaf7ee;border-color:#afd8bb}.alert.bad,.error{color:#a40000}.warning{color:#7a5200}.ok{color:#087443}.bad{color:#a40000}
</style>
"""

DASHBOARD_SCRIPT = """
<script>
let metrics=null;
const fmt=n=>Number(n||0).toLocaleString('ru-RU');
const uptime=s=>{s=Number(s||0);const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return `${h}ч ${m}м`};
function card(name,value,extra=''){return `<div class="card"><span>${name}</span><b>${value}</b><small>${extra}</small></div>`}
function renderUsers(rows){
  const q=(document.querySelector('#userSearch')?.value||'').toLowerCase();
  const p=document.querySelector('#platformFilter')?.value||'', k=document.querySelector('#kindFilter')?.value||'';
  let filtered=rows.filter(u=>(!p||u.platform===p)&&(!q||JSON.stringify(u).toLowerCase().includes(q)));
  if(k==='teacher') filtered=filtered.filter(u=>u.subscription_type==='teacher');
  if(k==='group') filtered=filtered.filter(u=>u.subscription_type==='group');
  if(k==='new') filtered=filtered.filter(u=>u.is_new);
  if(k==='old') filtered=filtered.filter(u=>!u.is_new);
  const table=document.querySelector('#usersTable'); if(!table) return;
  table.innerHTML='<tr><th>Платформа</th><th>ID</th><th>Имя</th><th>Подписка</th><th>Создан</th><th>Последний визит</th></tr>'+filtered.map(u=>`<tr><td>${u.platform}</td><td>${u.user_id}</td><td>${u.full_name||u.username||''}</td><td>${u.subscription_title||'-'}<br><small>${u.subscription_type||''}</small></td><td>${u.created_at}</td><td>${u.last_seen_at}</td></tr>`).join('');
}
async function load(){
  const r=await fetch('/api/metrics'); metrics=await r.json();
  const o=document.querySelector('#overview'); if(o) o.innerHTML=[
    card('Аптайм',uptime(metrics.uptime_seconds)),card('Юзеров',fmt(metrics.users.total),`TG ${fmt(metrics.users.telegram)} / VK ${fmt(metrics.users.vk)}`),card('Новых за 7 дней',fmt(metrics.users.new_7d)),card('Тихих 30+ дней',fmt(metrics.extra.quiet_users))
  ].join('');
  const s=document.querySelector('#services'); if(s) s.innerHTML=Object.entries(metrics.services).map(([k,v])=>card(k, v.ok?'OK':'Проблема', v.label)).join('');
  renderUsers(metrics.user_rows||[]);
  const sch=document.querySelector('#schedule'); if(sch){sch.innerHTML=`<p>Последний парс: <b>${metrics.schedule.latest_parse?.created_at||'-'}</b> (${metrics.schedule.latest_parse?.source_title||'-'})</p><p>Последнее изменение: <b>${metrics.schedule.latest_change?.created_at||'-'}</b> (${metrics.schedule.latest_change?.source_title||'-'})</p><p>Активных групп: <b>${fmt(metrics.schedule.active_groups_total)}</b></p><div class="table-wrap"><table><tr><th>Группа</th><th>Юзеров</th></tr>${metrics.schedule.active_groups.map(g=>`<tr><td>${g.subscription_title}</td><td>${g.users_count}</td></tr>`).join('')}</table></div><h3>Изменения</h3><div class="table-wrap"><table><tr><th>Когда</th><th>Источник</th><th>Сообщение</th></tr>${metrics.schedule.changes.map(c=>`<tr><td>${c.created_at}</td><td>${c.source_title||''}</td><td>${(c.message||'').slice(0,240)}</td></tr>`).join('')}</table></div>`}
  const d=document.querySelector('#delivery'); if(d){const t=metrics.delivery.today,a=metrics.delivery.total;d.innerHTML=[card('Сегодня доставлено',fmt(t.sent),`Rabbit ${fmt(t.via_broker)} / direct ${fmt(t.direct)}`),card('Сегодня TG/VK',`${fmt(t.telegram)} / ${fmt(t.vk)}`,`ошибок ${fmt(t.failed)}`),card('Всего доставлено',fmt(a.sent),`Rabbit ${fmt(a.via_broker)} / direct ${fmt(a.direct)}`),card('Всего TG/VK',`${fmt(a.telegram)} / ${fmt(a.vk)}`,`ошибок ${fmt(a.failed)}`)].join('')}
  const l=document.querySelector('#lessonsStats'); if(l){const lc=metrics.lesson_counters;l.innerHTML=`<p>Настроено счетчиков: <b>${fmt(lc.configured)}</b>, групп: <b>${fmt(lc.groups)}</b>, учтено сегодня: <b>${fmt(lc.counted_today)}</b></p><p>Последний учет: <b>${lc.last_event?.created_at||'-'}</b></p><div class="table-wrap"><table><tr><th>Группа</th><th>Предмет</th><th>Преподаватель</th><th>Прогресс</th></tr>${lc.counters.map(c=>`<tr><td>${c.schedule_id}</td><td>${c.subject}</td><td>${c.teacher}</td><td>${c.passed_count}/${c.total_count}</td></tr>`).join('')}</table></div>`}
}
document.addEventListener('input',e=>{if(['userSearch','platformFilter','kindFilter'].includes(e.target.id)&&metrics)renderUsers(metrics.user_rows||[])});
load(); setInterval(load,30000);
</script>
"""
