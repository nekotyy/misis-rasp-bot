import asyncio
import contextlib
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

import aio_pika
import httpx

from src.db import Database

logger = logging.getLogger(__name__)

STARTED_AT = datetime.now()


def get_bot_version() -> str:
    """Reads bot version from pyproject.toml or returns default."""
    with contextlib.suppress(Exception):
        pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        if pyproject_path.exists():
            text = pyproject_path.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("version ="):
                    return stripped.split("=", 1)[1].strip(" \"'")
    return "1.4.0"


BOT_VERSION = get_bot_version()


def get_uptime(started_at: datetime | None = None) -> tuple[int, int, int, int]:
    """Returns (days, hours, minutes, seconds) since start."""
    start = started_at or STARTED_AT
    delta = datetime.now() - start
    total_seconds = max(0, int(delta.total_seconds()))
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return days, hours, minutes, seconds


def format_uptime(started_at: datetime | None = None) -> str:
    """Formats uptime as human readable Russian string."""
    days, hours, minutes, _ = get_uptime(started_at)
    if days > 0:
        return f"{days} дн. {hours} ч. {minutes} мин."
    if hours > 0:
        return f"{hours} ч. {minutes} мин."
    return f"{minutes} мин."


def get_memory_usage_mb() -> float:
    """Returns current process RSS memory in megabytes."""
    with contextlib.suppress(Exception):
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            fn = getattr(ctypes.windll.kernel32, "K32GetProcessMemoryInfo", None) or ctypes.windll.psapi.GetProcessMemoryInfo
            fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
            fn.restype = wintypes.BOOL
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if fn(handle, ctypes.byref(counters), counters.cb):
                return round(counters.WorkingSetSize / (1024 * 1024), 1)
        else:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # On Linux ru_maxrss is in kilobytes, on macOS in bytes
            if sys.platform == "darwin":
                return round(usage / (1024 * 1024), 1)
            return round(usage / 1024, 1)
    return 0.0


def format_bytes(bytes_count: int) -> str:
    if bytes_count < 1024:
        return f"{bytes_count} Б"
    if bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.1f} КБ"
    return f"{bytes_count / (1024 * 1024):.2f} МБ"


COMPONENT_TITLES = {
    "schedule_site": "Сайт расписания МИСИС",
    "rabbitmq": "RabbitMQ Брокер",
    "web_dashboard": "Веб-дашборд (Борда)",
    "telegram": "Telegram Bot API",
    "vk": "VK Bot API",
    "database": "База данных SQLite",
    "scheduler": "Фоновый планировщик",
    "delivery": "Служба доставки уведомлений",
    "ocr": "Распознавание расписания с фото",
}


async def check_ocr_status(ocr_importer: Any) -> dict[str, Any]:
    """Состояние движка распознавания фото.

    Готовым считается только прогретый движок: пока модели не подняты, первое
    фото будет ждать их загрузки десятки секунд.
    """
    now = datetime.now().isoformat()
    if ocr_importer is None:
        return {"ok": False, "ready": False, "engine": "", "error": "Импорт с фото не настроен", "checked_at": now}

    try:
        available, message = ocr_importer.availability()
    except Exception as exc:
        return {"ok": False, "ready": False, "engine": "", "error": type(exc).__name__, "checked_at": now}

    engine = getattr(getattr(ocr_importer, "engine", None), "name", "") or ""
    if not available:
        return {"ok": False, "ready": False, "engine": engine, "error": message, "checked_at": now}

    ready = bool(getattr(ocr_importer, "is_warm", False))
    return {
        "ok": True,
        "ready": ready,
        "engine": engine,
        "error": None if ready else "Модели ещё греются",
        "details": message,
        "checked_at": now,
    }


async def check_schedule_site(schedule_url: str, timeout: float = 6.0) -> dict[str, Any]:
    """Checks accessibility of the MISIS schedule site."""
    if not schedule_url:
        return {"ok": False, "status_code": 0, "latency_ms": 0, "error": "URL не задан", "checked_at": datetime.now().isoformat()}
    url = schedule_url.strip()
    t0 = monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url)
            latency_ms = int((monotonic() - t0) * 1000)
            status_code = response.status_code
            is_ok = 200 <= status_code < 400
            error = None if is_ok else f"HTTP {status_code}"
            return {
                "ok": is_ok,
                "status_code": status_code,
                "latency_ms": latency_ms,
                "error": error,
                "checked_at": datetime.now().isoformat(),
            }
    except httpx.TimeoutException:
        latency_ms = int((monotonic() - t0) * 1000)
        return {
            "ok": False,
            "status_code": 0,
            "latency_ms": latency_ms,
            "error": "Таймаут подключения (Timeout)",
            "checked_at": datetime.now().isoformat(),
        }
    except Exception as exc:
        latency_ms = int((monotonic() - t0) * 1000)
        return {
            "ok": False,
            "status_code": 0,
            "latency_ms": latency_ms,
            "error": type(exc).__name__,
            "checked_at": datetime.now().isoformat(),
        }


async def check_rabbitmq_status(rabbitmq_url: str, timeout: float = 4.0) -> dict[str, Any]:
    """Checks RabbitMQ broker connectivity."""
    if not rabbitmq_url:
        return {"ok": False, "label": "disabled", "error": "URL не задан", "checked_at": datetime.now().isoformat()}
    connection = None
    t0 = monotonic()
    try:
        connection = await aio_pika.connect_robust(rabbitmq_url, timeout=timeout)
        latency_ms = int((monotonic() - t0) * 1000)
        return {
            "ok": True,
            "label": "connected",
            "latency_ms": latency_ms,
            "error": None,
            "checked_at": datetime.now().isoformat(),
        }
    except Exception as exc:
        latency_ms = int((monotonic() - t0) * 1000)
        return {
            "ok": False,
            "label": "disconnected",
            "latency_ms": latency_ms,
            "error": type(exc).__name__,
            "checked_at": datetime.now().isoformat(),
        }
    finally:
        if connection is not None and not connection.is_closed:
            with contextlib.suppress(Exception):
                await connection.close()


async def check_web_dashboard_status(port: int = 8080, host: str = "127.0.0.1", timeout: float = 3.0) -> dict[str, Any]:
    """Checks local web configurator / dashboard responsiveness."""
    url = f"http://{host}:{port}/login"
    t0 = monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            latency_ms = int((monotonic() - t0) * 1000)
            is_ok = response.status_code in {200, 302, 307}
            return {
                "ok": is_ok,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "error": None if is_ok else f"HTTP {response.status_code}",
                "checked_at": datetime.now().isoformat(),
            }
    except Exception as exc:
        latency_ms = int((monotonic() - t0) * 1000)
        return {
            "ok": False,
            "status_code": 0,
            "latency_ms": latency_ms,
            "error": type(exc).__name__,
            "checked_at": datetime.now().isoformat(),
        }


async def check_telegram_status(token: str, timeout: float = 4.0) -> dict[str, Any]:
    """Checks Telegram Bot API token validity and reachability."""
    if not token:
        return {"ok": False, "label": "token missing", "error": "Токен не задан", "checked_at": datetime.now().isoformat()}
    t0 = monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            latency_ms = int((monotonic() - t0) * 1000)
            data = response.json()
            is_ok = bool(data.get("ok"))
            username = data.get("result", {}).get("username")
            return {
                "ok": is_ok,
                "label": f"@{username}" if username else ("OK" if is_ok else "Error"),
                "latency_ms": latency_ms,
                "error": None if is_ok else str(data.get("description", "Unknown error")),
                "checked_at": datetime.now().isoformat(),
            }
    except Exception as exc:
        latency_ms = int((monotonic() - t0) * 1000)
        return {
            "ok": False,
            "label": type(exc).__name__,
            "latency_ms": latency_ms,
            "error": type(exc).__name__,
            "checked_at": datetime.now().isoformat(),
        }


async def check_vk_status(token: str, timeout: float = 4.0) -> dict[str, Any]:
    """Checks VK Bot API token validity and reachability."""
    if not token:
        return {"ok": False, "label": "token missing", "error": "Токен не задан", "checked_at": datetime.now().isoformat()}
    t0 = monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                "https://api.vk.com/method/groups.getById",
                params={"access_token": token, "v": "5.199"},
            )
            latency_ms = int((monotonic() - t0) * 1000)
            data = response.json()
            is_ok = "error" not in data
            return {
                "ok": is_ok,
                "label": "OK" if is_ok else str(data.get("error", {}).get("error_msg", "Error")),
                "latency_ms": latency_ms,
                "error": None if is_ok else str(data.get("error", {}).get("error_msg")),
                "checked_at": datetime.now().isoformat(),
            }
    except Exception as exc:
        latency_ms = int((monotonic() - t0) * 1000)
        return {
            "ok": False,
            "label": type(exc).__name__,
            "latency_ms": latency_ms,
            "error": type(exc).__name__,
            "checked_at": datetime.now().isoformat(),
        }


async def check_database_status(db: Database) -> dict[str, Any]:
    """Checks SQLite database file and connectivity."""
    db_size = db.path.stat().st_size if db.path.exists() else 0
    t0 = monotonic()
    try:
        users = await db.list_users()
        latency_ms = int((monotonic() - t0) * 1000)
        return {
            "ok": True,
            "size_bytes": db_size,
            "size_formatted": format_bytes(db_size),
            "users_count": len(users),
            "latency_ms": latency_ms,
            "error": None,
            "checked_at": datetime.now().isoformat(),
        }
    except Exception as exc:
        latency_ms = int((monotonic() - t0) * 1000)
        return {
            "ok": False,
            "size_bytes": db_size,
            "size_formatted": format_bytes(db_size),
            "users_count": 0,
            "latency_ms": latency_ms,
            "error": str(exc),
            "checked_at": datetime.now().isoformat(),
        }


class SystemAlertManager:
    """Manages service state changes, outage tracking and admin alerts."""

    def __init__(
        self,
        db: Database,
        broadcaster: Any | None = None,
        cooldown_seconds: float = 1800.0,  # 30 minutes repeat reminder
    ) -> None:
        self.db = db
        self.broadcaster = broadcaster
        self.cooldown_seconds = cooldown_seconds
        # component -> {"ok": bool, "down_since": datetime, "last_alert_at": float, "last_error": str}
        self._states: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def report_component_status(
        self,
        component: str,
        ok: bool,
        error_message: str | None = None,
        details: str | None = None,
    ) -> None:
        async with self._lock:
            now_dt = datetime.now()
            now_mono = monotonic()
            state = self._states.get(component)
            comp_name = COMPONENT_TITLES.get(component, component)

            if state is None:
                # First observation
                self._states[component] = {
                    "ok": ok,
                    "down_since": now_dt if not ok else None,
                    "last_alert_at": now_mono if not ok else 0.0,
                    "last_error": error_message or "",
                }
                if not ok:
                    await self._record_and_alert_down(component, comp_name, error_message, details, now_dt)
                return

            was_ok = state["ok"]

            if was_ok and not ok:
                # Service went DOWN
                state["ok"] = False
                state["down_since"] = now_dt
                state["last_alert_at"] = now_mono
                state["last_error"] = error_message or ""
                await self._record_and_alert_down(component, comp_name, error_message, details, now_dt)

            elif not was_ok and ok:
                # Service recovered UP
                down_since = state.get("down_since") or now_dt
                duration = now_dt - down_since
                state["ok"] = True
                state["down_since"] = None
                state["last_alert_at"] = 0.0
                state["last_error"] = ""
                await self._alert_recovery(comp_name, duration, now_dt)

            elif not was_ok and not ok:
                # Still down - check cooldown for reminder
                last_alert = state.get("last_alert_at", 0.0)
                if now_mono - last_alert >= self.cooldown_seconds:
                    state["last_alert_at"] = now_mono
                    state["last_error"] = error_message or ""
                    await self._record_and_alert_down(
                        component,
                        comp_name,
                        error_message,
                        details,
                        now_dt,
                        is_reminder=True,
                    )

    async def _record_and_alert_down(
        self,
        component: str,
        comp_name: str,
        error_message: str | None,
        details: str | None,
        now_dt: datetime,
        is_reminder: bool = False,
    ) -> None:
        err_msg = error_message or "Неизвестный сбой"
        await self.db.log_system_error(
            component=component,
            error_type="outage" if not is_reminder else "outage_reminder",
            message=err_msg,
            details=details,
            created_at=now_dt.isoformat(timespec="seconds"),
        )
        logger.error("System component failure [%s]: %s (%s)", component, err_msg, details)

        if self.broadcaster is None:
            return

        time_str = now_dt.strftime("%d.%m.%Y %H:%M:%S")
        prefix = "⚠️ <b>НАПОМИНАНИЕ: Служба все еще недоступна!</b>" if is_reminder else "🚨 <b>ВНИМАНИЕ: Сбой службы бота!</b>"
        vk_prefix = "⚠️ НАПОМИНАНИЕ: Служба все еще недоступна!" if is_reminder else "🚨 ВНИМАНИЕ: Сбой службы бота!"

        tg_text = "\n".join([
            prefix,
            "───────────────────────────",
            f"🛠️ <b>Служба:</b> {comp_name}",
            f"❌ <b>Ошибка:</b> <code>{err_msg}</code>",
            f"🕒 <b>Время:</b> {time_str}",
            f"ℹ️ <b>Детали:</b> {details or 'Автоматический мониторинг зафиксировал сбой.'}",
        ])
        vk_text = "\n".join([
            vk_prefix,
            "───────────────────────────",
            f"Служба: {comp_name}",
            f"Ошибка: {err_msg}",
            f"Время: {time_str}",
            f"Детали: {details or 'Автоматический мониторинг зафиксировал сбой.'}",
        ])

        try:
            await self.broadcaster.notify_admins(telegram_message=tg_text, vk_message=vk_text)
        except Exception as exc:
            logger.warning("Failed to notify admins of component outage [%s]: %s", component, exc)

    async def _alert_recovery(self, comp_name: str, duration: timedelta, now_dt: datetime) -> None:
        logger.info("System component recovered [%s] after %s", comp_name, duration)
        if self.broadcaster is None:
            return

        time_str = now_dt.strftime("%d.%m.%Y %H:%M:%S")
        total_seconds = max(0, int(duration.total_seconds()))
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        duration_str = f"{minutes} мин. {seconds} сек." if minutes > 0 else f"{seconds} сек."

        tg_text = "\n".join([
            "✅ <b>СЛУЖБА ВОССТАНОВЛЕНА!</b>",
            "───────────────────────────",
            f"🛠️ <b>Служба:</b> {comp_name}",
            f"🕒 <b>Время восстановления:</b> {time_str}",
            f"⏱️ <b>Длительность сбоя:</b> {duration_str}",
            "Все системы работают в штатном режиме.",
        ])
        vk_text = "\n".join([
            "✅ СЛУЖБА ВОССТАНОВЛЕНА!",
            "───────────────────────────",
            f"Служба: {comp_name}",
            f"Время восстановления: {time_str}",
            f"Длительность сбоя: {duration_str}",
            "Все системы работают в штатном режиме.",
        ])

        try:
            await self.broadcaster.notify_admins(telegram_message=tg_text, vk_message=vk_text)
        except Exception as exc:
            logger.warning("Failed to notify admins of component recovery [%s]: %s", comp_name, exc)


async def format_daily_errors_report(db: Database, html: bool = True, date_prefix: str | None = None) -> str:
    """Builds a formatted text report of today's errors."""
    today = date_prefix or datetime.now().date().isoformat()
    summary = await db.get_daily_errors_summary(today)
    errors = await db.get_daily_errors(today, limit=30)

    total = summary["total_errors"]
    sys_total = summary["system_errors_total"]
    deliv_total = summary["delivery_errors_total"]

    if html:
        lines = [
            f"⚠️ <b>Ошибки и сбои за сегодня ({today})</b>",
            "───────────────────────────",
            f"• <b>Всего ошибок:</b> <b>{total}</b> (Службы/системы: {sys_total}, Доставка: {deliv_total})",
        ]
        if summary["by_component"]:
            comp_parts = [f"{COMPONENT_TITLES.get(k, k)}: <b>{v}</b>" for k, v in summary["by_component"].items()]
            lines.append(f"• <b>Службы:</b> {', '.join(comp_parts)}")
        if summary["by_delivery_platform"]:
            plat_parts = [f"{k.upper()}: <b>{v}</b>" for k, v in summary["by_delivery_platform"].items()]
            lines.append(f"• <b>Доставка:</b> {', '.join(plat_parts)}")

        lines.append("───────────────────────────")
        if not errors:
            lines.append("🎉 <i>За сегодня ошибок не зафиксировано. Все службы работают штатно!</i>")
            return "\n".join(lines)

        lines.append("<b>Последние события:</b>")
        for idx, err in enumerate(errors[:15], start=1):
            time_part = err.get("created_at", "").split("T")[-1][:8] if "T" in str(err.get("created_at", "")) else str(err.get("created_at", ""))
            if err.get("kind") == "system":
                comp = COMPONENT_TITLES.get(err.get("component"), err.get("component"))
                msg = err.get("message") or "Ошибка"
                lines.append(f"{idx}. <b>[{time_part}]</b> 🛠️ <b>{comp}</b>: <code>{msg}</code>")
            else:
                plat = str(err.get("platform", "")).upper()
                msg = err.get("message") or "Сбой отправки"
                lines.append(f"{idx}. <b>[{time_part}]</b> 📬 <b>{plat}</b>: <code>{msg}</code>")

        if len(errors) > 15:
            lines.append(f"\n<i>... и ещё {len(errors) - 15} событий.</i>")
        return "\n".join(lines)
    else:
        lines = [
            f"⚠️ Ошибки и сбои за сегодня ({today})",
            "───────────────────────────",
            f"• Всего ошибок: {total} (Службы: {sys_total}, Доставка: {deliv_total})",
        ]
        if summary["by_component"]:
            comp_parts = [f"{COMPONENT_TITLES.get(k, k)}: {v}" for k, v in summary["by_component"].items()]
            lines.append(f"• Службы: {', '.join(comp_parts)}")
        if summary["by_delivery_platform"]:
            plat_parts = [f"{k.upper()}: {v}" for k, v in summary["by_delivery_platform"].items()]
            lines.append(f"• Доставка: {', '.join(plat_parts)}")

        lines.append("───────────────────────────")
        if not errors:
            lines.append("🎉 За сегодня ошибок не зафиксировано. Все службы работают штатно!")
            return "\n".join(lines)

        lines.append("Последние события:")
        for idx, err in enumerate(errors[:15], start=1):
            time_part = err.get("created_at", "").split("T")[-1][:8] if "T" in str(err.get("created_at", "")) else str(err.get("created_at", ""))
            if err.get("kind") == "system":
                comp = COMPONENT_TITLES.get(err.get("component"), err.get("component"))
                msg = err.get("message") or "Ошибка"
                lines.append(f"{idx}. [{time_part}] 🛠️ {comp}: {msg}")
            else:
                plat = str(err.get("platform", "")).upper()
                msg = err.get("message") or "Сбой отправки"
                lines.append(f"{idx}. [{time_part}] 📬 {plat}: {msg}")

        if len(errors) > 15:
            lines.append(f"\n... и ещё {len(errors) - 15} событий.")
        return "\n".join(lines)
