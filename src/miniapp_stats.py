"""Сбор и форматирование телеметрии и статистики Mini App без эмодзи."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import aiosqlite

if TYPE_CHECKING:
    from src.db import Database

logger = logging.getLogger(__name__)

RUSSIAN_WEEKDAYS = {
    0: "Понедельник",
    1: "Вторник",
    2: "Среда",
    3: "Четверг",
    4: "Пятница",
    5: "Суббота",
    6: "Воскресенье",
}


@dataclass(frozen=True, slots=True)
class MiniappStatsReport:
    generated_at: str

    # Уникальные пользователи
    total_unique_users: int
    total_tg_unique: int
    total_vk_unique: int
    dau_today: int
    wau_7d: int
    mau_30d: int

    # Сессии / открытия
    total_sessions: int
    sessions_today: int
    sessions_7d: int
    sessions_30d: int
    active_online_15m: int

    # Длительность сессий
    avg_duration_seconds: int
    median_duration_seconds: int
    total_time_spent_seconds: int

    # Пики активности
    peak_hour: str
    peak_hour_sessions: int
    top_active_hours: list[tuple[str, int]]
    peak_day_of_week: str
    peak_day_sessions: int

    # Разделы (просмотры страниц)
    page_views_schedule: int
    page_views_history: int
    page_views_profile: int

    # Топ просматриваемых групп
    top_groups: list[tuple[str, int]]

    # Оформление интерфейса
    dark_theme_count: int
    light_theme_count: int
    scale_100_count: int
    scale_other_count: int


@asynccontextmanager
async def _as_connection(db: aiosqlite.Connection | Database | Any) -> AsyncIterator[aiosqlite.Connection]:
    """Обеспечивает работу как с aiosqlite.Connection, так и с экземпляром Database."""
    if isinstance(db, aiosqlite.Connection):
        db.row_factory = aiosqlite.Row
        yield db
    elif hasattr(db, "_connect"):
        async with db._connect() as conn:
            conn.row_factory = aiosqlite.Row
            yield conn
    elif hasattr(db, "execute"):
        db.row_factory = aiosqlite.Row
        yield db
    else:
        raise TypeError(f"Expected aiosqlite.Connection or Database, got {type(db)}")


async def record_telemetry_session(
    db: aiosqlite.Connection | Database | Any,
    *,
    session_id: str,
    platform: str,
    user_id: int,
    account_id: str | None = None,
    theme: str = "system",
    scale: int = 100,
    active_group: str | None = None,
    last_page: str = "/schedule",
) -> None:
    """Регистрирует запуск сессии миниаппа."""
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    async with _as_connection(db) as conn:
        await conn.execute(
            """
            INSERT INTO miniapp_telemetry_sessions
            (id, account_id, platform, user_id, theme, scale, start_time, last_ping_time, duration_seconds, active_group, last_page, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_ping_time = excluded.last_ping_time,
                theme = excluded.theme,
                scale = excluded.scale,
                active_group = COALESCE(excluded.active_group, active_group),
                last_page = COALESCE(excluded.last_page, last_page)
            """,
            (session_id, account_id, platform, user_id, theme, scale, now_iso, now_iso, active_group, last_page, now_iso),
        )
        await conn.execute(
            """
            INSERT INTO miniapp_telemetry_events
            (session_id, account_id, platform, user_id, event_type, event_data, created_at)
            VALUES (?, ?, ?, ?, 'open', ?, ?)
            """,
            (session_id, account_id, platform, user_id, json.dumps({"page": last_page, "group": active_group}, ensure_ascii=False), now_iso),
        )
        await conn.commit()


async def record_telemetry_ping(
    db: aiosqlite.Connection | Database | Any,
    *,
    session_id: str,
    duration_seconds: int = 0,
    active_group: str | None = None,
    current_page: str | None = None,
) -> None:
    """Обновляет время активности и длительность сессии."""
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    async with _as_connection(db) as conn:
        await conn.execute(
            """
            UPDATE miniapp_telemetry_sessions
            SET
                last_ping_time = ?,
                duration_seconds = MAX(duration_seconds, ?),
                active_group = COALESCE(?, active_group),
                last_page = COALESCE(?, last_page)
            WHERE id = ?
            """,
            (now_iso, max(0, duration_seconds), active_group, current_page, session_id),
        )
        await conn.commit()


async def record_telemetry_event(
    db: aiosqlite.Connection | Database | Any,
    *,
    session_id: str,
    event_type: str,
    event_data: dict[str, Any] | None = None,
    platform: str = "unknown",
    user_id: int = 0,
    account_id: str | None = None,
) -> None:
    """Регистрирует событие интерфейса (переход, выбор группы и т.д.)."""
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    payload = json.dumps(event_data or {}, ensure_ascii=False)
    async with _as_connection(db) as conn:
        await conn.execute(
            """
            INSERT INTO miniapp_telemetry_events
            (session_id, account_id, platform, user_id, event_type, event_data, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, account_id, platform, user_id, event_type, payload, now_iso),
        )
        await conn.commit()


async def collect_miniapp_stats(db: aiosqlite.Connection | Database | Any) -> MiniappStatsReport:
    """Собирает полную сводку телеметрии Mini App из базы данных."""
    tz_moscow = ZoneInfo("Europe/Moscow")
    now_moscow = datetime.now(tz_moscow)
    now_utc = datetime.now(UTC)

    # Границы в UTC для консистентного сравнения строковых ISO-меток
    today_start_dt = datetime.combine(now_moscow.date(), datetime.min.time(), tzinfo=tz_moscow).astimezone(UTC)
    today_start = today_start_dt.isoformat(timespec="seconds")
    seven_days_ago = (now_utc - timedelta(days=7)).isoformat(timespec="seconds")
    thirty_days_ago = (now_utc - timedelta(days=30)).isoformat(timespec="seconds")
    fifteen_mins_ago = (now_utc - timedelta(minutes=15)).isoformat(timespec="seconds")

    async with _as_connection(db) as conn:

        # 1. Уникальные пользователи по всем доступным источникам (telemetry + sessions + identities)
        # Всего уникальных (Telegram + VK)
        cur = await conn.execute(
            """
            WITH all_users AS (
                SELECT platform, user_id, start_time AS ts FROM miniapp_telemetry_sessions
                UNION
                SELECT platform, user_id, created_at AS ts FROM miniapp_sessions
                UNION
                SELECT platform, user_id, created_at AS ts FROM account_identities
            )
            SELECT
                COUNT(DISTINCT platform || ':' || user_id) AS total_unique,
                COUNT(DISTINCT CASE WHEN platform = 'telegram' THEN user_id END) AS tg_unique,
                COUNT(DISTINCT CASE WHEN platform = 'vk' THEN user_id END) AS vk_unique,
                COUNT(DISTINCT CASE WHEN ts >= ? THEN platform || ':' || user_id END) AS dau,
                COUNT(DISTINCT CASE WHEN ts >= ? THEN platform || ':' || user_id END) AS wau,
                COUNT(DISTINCT CASE WHEN ts >= ? THEN platform || ':' || user_id END) AS mau
            FROM all_users
            """,
            (today_start, seven_days_ago, thirty_days_ago),
        )
        user_row = await cur.fetchone()
        total_unique = user_row["total_unique"] if user_row else 0
        tg_unique = user_row["tg_unique"] if user_row else 0
        vk_unique = user_row["vk_unique"] if user_row else 0
        dau = user_row["dau"] if user_row else 0
        wau = user_row["wau"] if user_row else 0
        mau = user_row["mau"] if user_row else 0

        # 2. Сессии и запуски
        cur_sess = await conn.execute(
            """
            WITH combined_sessions AS (
                SELECT id, start_time AS ts, last_ping_time, duration_seconds FROM miniapp_telemetry_sessions
                UNION ALL
                SELECT token_hash AS id, created_at AS ts, created_at AS last_ping_time, 0 AS duration_seconds
                FROM miniapp_sessions
                WHERE token_hash NOT IN (SELECT id FROM miniapp_telemetry_sessions)
            )
            SELECT
                COUNT(*) AS total_sessions,
                COUNT(CASE WHEN ts >= ? THEN 1 END) AS sessions_today,
                COUNT(CASE WHEN ts >= ? THEN 1 END) AS sessions_7d,
                COUNT(CASE WHEN ts >= ? THEN 1 END) AS sessions_30d,
                COUNT(CASE WHEN last_ping_time >= ? THEN 1 END) AS online_15m
            FROM combined_sessions
            """,
            (today_start, seven_days_ago, thirty_days_ago, fifteen_mins_ago),
        )
        sess_row = await cur_sess.fetchone()
        total_sessions = sess_row["total_sessions"] if sess_row else 0
        sessions_today = sess_row["sessions_today"] if sess_row else 0
        sessions_7d = sess_row["sessions_7d"] if sess_row else 0
        sessions_30d = sess_row["sessions_30d"] if sess_row else 0
        online_15m = sess_row["online_15m"] if sess_row else 0

        # 3. Среднее время пребывания в приложении
        cur_dur = await conn.execute(
            """
            SELECT duration_seconds
            FROM miniapp_telemetry_sessions
            WHERE duration_seconds > 0
            ORDER BY duration_seconds ASC
            """
        )
        durations = [row[0] for row in await cur_dur.fetchall()]
        if durations:
            avg_dur = sum(durations) // len(durations)
            median_dur = durations[len(durations) // 2]
            total_time = sum(durations)
        else:
            avg_dur = 0
            median_dur = 0
            total_time = 0

        # 4. Пик активности: распределение по часам (по московскому времени UTC+3)
        cur_hours = await conn.execute(
            """
            WITH timestamps AS (
                SELECT start_time AS ts FROM miniapp_telemetry_sessions
                UNION ALL
                SELECT created_at AS ts FROM miniapp_sessions
            )
            SELECT
                strftime('%H', datetime(ts, '+3 hours')) AS hour,
                COUNT(*) AS cnt
            FROM timestamps
            WHERE ts IS NOT NULL AND ts != ''
            GROUP BY hour
            ORDER BY cnt DESC
            """
        )
        hour_rows = await cur_hours.fetchall()
        top_hours: list[tuple[str, int]] = []
        peak_hour = "Нет данных"
        peak_hour_cnt = 0
        for h_row in hour_rows:
            if h_row[0] is not None:
                h_int = int(h_row[0])
                h_label = f"{h_int:02d}:00–{(h_int + 1) % 24:02d}:00"
                cnt = int(h_row[1])
                if not top_hours:
                    peak_hour = h_label
                    peak_hour_cnt = cnt
                top_hours.append((h_label, cnt))
                if len(top_hours) >= 3:
                    break

        # Пик по дням недели
        cur_days = await conn.execute(
            """
            WITH timestamps AS (
                SELECT start_time AS ts FROM miniapp_telemetry_sessions
                UNION ALL
                SELECT created_at AS ts FROM miniapp_sessions
            )
            SELECT
                strftime('%w', datetime(ts, '+3 hours')) AS dow,
                COUNT(*) AS cnt
            FROM timestamps
            WHERE ts IS NOT NULL AND ts != ''
            GROUP BY dow
            ORDER BY cnt DESC
            LIMIT 1
            """
        )
        day_row = await cur_days.fetchone()
        peak_dow_str = "Нет данных"
        peak_dow_cnt = 0
        if day_row and day_row[0] is not None:
            # strftime %w: 0 = Sunday, 1 = Monday, ... 6 = Saturday
            sqlite_w = int(day_row[0])
            # Map to Russian weekday (0 = Monday .. 6 = Sunday)
            py_w = (sqlite_w - 1) % 7
            peak_dow_str = RUSSIAN_WEEKDAYS.get(py_w, "Нет данных")
            peak_dow_cnt = int(day_row[1])

        # 5. Просмотры разделов (расписание, история, профиль)
        cur_pages = await conn.execute(
            """
            SELECT
                COUNT(CASE WHEN event_data LIKE '%schedule%' OR last_page LIKE '%schedule%' THEN 1 END) AS sched,
                COUNT(CASE WHEN event_data LIKE '%history%' OR last_page LIKE '%history%' THEN 1 END) AS hist,
                COUNT(CASE WHEN event_data LIKE '%profile%' OR last_page LIKE '%profile%' THEN 1 END) AS prof
            FROM (
                SELECT event_data, '' AS last_page FROM miniapp_telemetry_events WHERE event_type = 'page_view'
                UNION ALL
                SELECT '' AS event_data, last_page FROM miniapp_telemetry_sessions WHERE last_page IS NOT NULL
            )
            """
        )
        pages_row = await cur_pages.fetchone()
        views_sched = pages_row["sched"] if pages_row else 0
        views_hist = pages_row["hist"] if pages_row else 0
        views_prof = pages_row["prof"] if pages_row else 0

        # 6. Топ просматриваемых групп в Mini App
        cur_groups = await conn.execute(
            """
            WITH group_mentions AS (
                SELECT active_group AS grp FROM miniapp_telemetry_sessions WHERE active_group IS NOT NULL AND active_group != ''
                UNION ALL
                SELECT group_name AS grp FROM account_subscriptions WHERE group_name IS NOT NULL AND group_name != ''
            )
            SELECT grp, COUNT(*) AS cnt
            FROM group_mentions
            GROUP BY grp
            ORDER BY cnt DESC
            LIMIT 5
            """
        )
        top_groups = [(row[0], int(row[1])) for row in await cur_groups.fetchall()]

        # 7. Темы и масштабы оформления
        cur_theme = await conn.execute(
            """
            SELECT
                COUNT(CASE WHEN theme IN ('dark', 'system') THEN 1 END) AS dark_cnt,
                COUNT(CASE WHEN theme = 'light' THEN 1 END) AS light_cnt
            FROM (
                SELECT theme FROM miniapp_telemetry_sessions
                UNION ALL
                SELECT theme FROM accounts
            )
            """
        )
        theme_row = await cur_theme.fetchone()
        dark_cnt = theme_row["dark_cnt"] if theme_row else 0
        light_cnt = theme_row["light_cnt"] if theme_row else 0

        cur_scale = await conn.execute(
            """
            SELECT
                COUNT(CASE WHEN scale = 100 THEN 1 END) AS s100,
                COUNT(CASE WHEN scale != 100 THEN 1 END) AS s_other
            FROM miniapp_telemetry_sessions
            """
        )
        scale_row = await cur_scale.fetchone()
        s100_cnt = scale_row["s100"] if scale_row else 0
        s_other_cnt = scale_row["s_other"] if scale_row else 0

        return MiniappStatsReport(
            generated_at=now_moscow.strftime("%d.%m.%Y %H:%M"),
            total_unique_users=total_unique,
            total_tg_unique=tg_unique,
            total_vk_unique=vk_unique,
            dau_today=dau,
            wau_7d=wau,
            mau_30d=mau,
            total_sessions=total_sessions,
            sessions_today=sessions_today,
            sessions_7d=sessions_7d,
            sessions_30d=sessions_30d,
            active_online_15m=online_15m,
            avg_duration_seconds=avg_dur,
            median_duration_seconds=median_dur,
            total_time_spent_seconds=total_time,
            peak_hour=peak_hour,
            peak_hour_sessions=peak_hour_cnt,
            top_active_hours=top_hours,
            peak_day_of_week=peak_dow_str,
            peak_day_sessions=peak_dow_cnt,
            page_views_schedule=views_sched,
            page_views_history=views_hist,
            page_views_profile=views_prof,
            top_groups=top_groups,
            dark_theme_count=dark_cnt,
            light_theme_count=light_cnt,
            scale_100_count=s100_cnt,
            scale_other_count=s_other_cnt,
        )


def format_duration(seconds: int) -> str:
    """Форматирует секунды в человекочитаемый вид без эмодзи."""
    if seconds <= 0:
        return "0 сек"
    if seconds < 60:
        return f"{seconds} сек"
    minutes = seconds // 60
    rem_sec = seconds % 60
    if minutes < 60:
        return f"{minutes} мин {rem_sec} сек" if rem_sec > 0 else f"{minutes} мин"
    hours = minutes // 60
    rem_min = minutes % 60
    return f"{hours} ч {rem_min} мин"


def format_miniapp_stats_report(report: MiniappStatsReport, *, html: bool = True) -> str:
    """Форматирует отчет о статистике Mini App. СТРОГО БЕЗ ЭМОДЗИ."""

    def b(text: str) -> str:
        return f"<b>{text}</b>" if html else text

    total_views = report.page_views_schedule + report.page_views_history + report.page_views_profile
    sched_pct = f"{(report.page_views_schedule / total_views * 100):.1f}%" if total_views > 0 else "0%"
    hist_pct = f"{(report.page_views_history / total_views * 100):.1f}%" if total_views > 0 else "0%"
    prof_pct = f"{(report.page_views_profile / total_views * 100):.1f}%" if total_views > 0 else "0%"

    total_themes = report.dark_theme_count + report.light_theme_count
    dark_pct = f"{(report.dark_theme_count / total_themes * 100):.0f}%" if total_themes > 0 else "100%"
    light_pct = f"{(report.light_theme_count / total_themes * 100):.0f}%" if total_themes > 0 else "0%"

    total_scales = report.scale_100_count + report.scale_other_count
    scale_100_pct = f"{(report.scale_100_count / total_scales * 100):.0f}%" if total_scales > 0 else "100%"
    scale_other_pct = f"{(report.scale_other_count / total_scales * 100):.0f}%" if total_scales > 0 else "0%"

    top_hours_lines = []
    if report.top_active_hours:
        for hour_lbl, cnt in report.top_active_hours:
            top_hours_lines.append(f"  - {hour_lbl}: {cnt} запусков")
    else:
        top_hours_lines.append("  - Нет данных")

    top_groups_lines = []
    if report.top_groups:
        for grp, cnt in report.top_groups:
            top_groups_lines.append(f"• {grp}: {cnt} просмотров")
    else:
        top_groups_lines.append("• Данные пока не накоплены")

    lines = [
        b("Статистика Mini App"),
        "───────────────────────────",
        f"Данные на {report.generated_at}",
        "",
        b("Уникальные пользователи:"),
        f"• Всего за все время: {b(str(report.total_unique_users))}",
        f"  (Telegram: {report.total_tg_unique} / VK: {report.total_vk_unique})",
        f"• За сегодня (DAU): {b(str(report.dau_today))}",
        f"• За 7 дней (WAU): {b(str(report.wau_7d))}",
        f"• За 30 дней (MAU): {b(str(report.mau_30d))}",
        "",
        b("Сессии и запуски:"),
        f"• Всего запусков: {b(str(report.total_sessions))}",
        f"• За сегодня: {b(str(report.sessions_today))}",
        f"• За 7 дней: {b(str(report.sessions_7d))}",
        f"• За 30 дней: {b(str(report.sessions_30d))}",
        f"• Онлайн за последние 15 минут: {b(str(report.active_online_15m))}",
        "",
        b("Время в приложении:"),
        f"• Среднее время визита: {b(format_duration(report.avg_duration_seconds))}",
        f"• Медианное время визита: {b(format_duration(report.median_duration_seconds))}",
        f"• Суммарное время пользователей: {b(format_duration(report.total_time_spent_seconds))}",
        "",
        b("Пики активности:"),
        f"• Самый активный час: {b(report.peak_hour)} ({report.peak_hour_sessions} запусков)",
        "• Топ часов по активности:",
        *top_hours_lines,
        f"• Самый активный день недели: {b(report.peak_day_of_week)} ({report.peak_day_sessions} запусков)",
        "",
        b("Популярные разделы:"),
        f"• Расписание: {report.page_views_schedule} ({sched_pct})",
        f"• Архив и история: {report.page_views_history} ({hist_pct})",
        f"• Профиль и настройки: {report.page_views_profile} ({prof_pct})",
        "",
        b("Топ групп в Mini App:"),
        *top_groups_lines,
        "",
        b("Оформление интерфейса:"),
        f"• Темы: темная — {dark_pct}, светлая — {light_pct}",
        f"• Масштаб: 100% — {scale_100_pct}, другой — {scale_other_pct}",
        "───────────────────────────",
    ]
    return "\n".join(lines)
