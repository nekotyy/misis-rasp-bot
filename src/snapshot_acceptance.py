"""Общий безопасный путь административного обновления снимка."""

from __future__ import annotations

from typing import Any

from src.db import Database
from src.models import ScheduleSnapshot
from src.schedule_service import looks_like_stale_site_glitch


async def save_admin_snapshot(
    db: Database, source: dict[str, Any], snapshot: ScheduleSnapshot, snapshot_hash: str, *, snapshot_type: str,
) -> bool:
    if snapshot_type == "current":
        previous = await db.get_latest_snapshot(
            "current", schedule_id=source.get("schedule_id"), source_key=source.get("source_key"),
        )
        if looks_like_stale_site_glitch(previous, snapshot):
            return False
    await db.save_snapshot(
        snapshot_type, snapshot_hash, snapshot, source.get("schedule_id"), source.get("group_name"),
        source_type=source["source_type"], source_key=source["source_key"],
        source_title=source["source_title"], source_url=source["source_url"],
        archive_origin=("site" if source["source_type"] == "group" else "derived") if snapshot_type == "current" else None,
        archive_projected=True,
    )
    return True
