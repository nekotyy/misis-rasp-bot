from __future__ import annotations

from urllib.parse import urlsplit

from src.schedule_search import SearchTarget


def make_group_subscription(group_name: str, schedule_id: int) -> dict[str, str | int | None]:
    return {
        "subscription_type": "group",
        "subscription_key": f"group:{schedule_id}",
        "subscription_title": group_name,
        "subscription_url": f"rasp:{schedule_id}",
        "group_name": group_name,
        "schedule_id": schedule_id,
    }


def make_teacher_subscription(target: SearchTarget) -> dict[str, str | int | None]:
    teacher_id = extract_numeric_id(target.url)
    key = f"teacher:{teacher_id}" if teacher_id is not None else f"teacher:{target.url}"
    return {
        "subscription_type": "teacher",
        "subscription_key": key,
        "subscription_title": target.title,
        "subscription_url": target.url,
        "group_name": None,
        "schedule_id": None,
    }


def extract_numeric_id(url: str) -> int | None:
    path = urlsplit(url).path.rstrip("/")
    if not path:
        return None
    tail = path.rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def subscription_caption(subscription_type: str | None, subscription_title: str | None) -> str | None:
    if not subscription_type or not subscription_title:
        return None
    if subscription_type == "teacher":
        return f"Выбран преподаватель: {subscription_title}"
    return f"Твоя группа: {subscription_title}"

