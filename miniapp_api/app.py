"""FastAPI-приложение для Telegram Mini App и VK Mini App."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager, suppress
from datetime import date, timedelta
from pathlib import Path

import aiosqlite
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware

from miniapp_api.auth import (
    SessionManager,
    get_token_from_header,
    verify_telegram_init_data,
    verify_vk_launch_params,
)
from miniapp_api.dto import (
    AddSubscriptionRequest,
    AuthResponse,
    AuthTelegramRequest,
    AuthVkRequest,
    ConfirmLinkRequest,
    CreateLinkRequestResponse,
    DayScheduleDto,
    GroupItemDto,
    HistoryResponse,
    IdentityDto,
    LessonChangeDto,
    LessonDto,
    PreferencesDto,
    PreferencesUpdateRequest,
    ProfileResponse,
    ScheduleResponse,
    SubscriptionDto,
    UpdateSubscriptionRequest,
)
from src.account_service import Account, AccountIdentity, AccountService
from src.config import Settings
from src.db import Database
from src.group_catalog import GroupCatalog
from src.miniapp_calendar import (
    default_history_week,
    history_bounds,
    history_week_days,
    schedule_dates,
    today_in_timezone,
    week_start,
)
from src.miniapp_diff import DayChanges, compare_lessons
from src.models import DaySchedule, Lesson
from src.schedule_archive import ScheduleDayArchive
from src.university_subgroups import filter_day


def get_db_path() -> Path:
    raw = os.getenv("DATABASE_PATH")
    if raw:
        return Path(raw)
    return Settings.from_env().database_path


@asynccontextmanager
async def lifespan(app: FastAPI):
    path = get_db_path()
    db = Database(path)
    await db.initialize()
    yield


app = FastAPI(
    title="MISIS Rasp Mini App API",
    version="1.0.0",
    docs_url=None,  # Документация не публикуется наружу per security guidelines
    redoc_url=None,
    lifespan=lifespan,
)

# Разрешаем CORS для клиентов Telegram WebApp и VK Mini App
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
@app.get("/api/health")
async def health_check():
    return {"status": "ok"}


async def get_db():
    path = get_db_path()
    async with aiosqlite.connect(path, timeout=30.0) as db:
        yield db


async def get_current_user_and_account(
    token: str = Depends(get_token_from_header),
    db: aiosqlite.Connection = Depends(get_db),
) -> tuple[Account, AccountIdentity]:
    auth = await SessionManager.authenticate_session(db, token)
    if not auth:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Сессия недействительна или истекла",
        )
    return auth


# --- Маршруты авторизации ---

@app.post("/api/miniapp/v1/auth/telegram", response_model=AuthResponse)
async def auth_telegram(req: AuthTelegramRequest, db: aiosqlite.Connection = Depends(get_db)):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN") or Settings.from_env().telegram_bot_token
    dev_mode = os.getenv("MINIAPP_DEV_MODE", "0") == "1"

    verified = verify_telegram_init_data(req.init_data, bot_token)
    if not verified:
        if dev_mode and req.init_data.startswith("dev:"):
            # Режим разработки: dev:<user_id>:<username>
            parts = req.init_data.split(":")
            uid = int(parts[1]) if len(parts) > 1 else 10001
            uname = parts[2] if len(parts) > 2 else "dev_tg"
            verified = {"user_id": uid, "username": uname, "first_name": "Dev", "last_name": "TG"}
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Недействительные параметры запуска Telegram",
            )

    full_name = f"{verified.get('first_name') or ''} {verified.get('last_name') or ''}".strip() or None
    account = await AccountService.get_or_create_account(
        db,
        platform="telegram",
        user_id=verified["user_id"],
        username=verified.get("username"),
        full_name=full_name,
    )
    token = await SessionManager.create_session(db, account.id, "telegram", verified["user_id"])
    await db.commit()
    return AuthResponse(token=token, platform="telegram", user_id=verified["user_id"], account_id=account.id)


@app.post("/api/miniapp/v1/auth/vk", response_model=AuthResponse)
async def auth_vk(req: AuthVkRequest, db: aiosqlite.Connection = Depends(get_db)):
    client_secret = os.getenv("VK_APP_SECRET", "")
    dev_mode = os.getenv("MINIAPP_DEV_MODE", "0") == "1"

    verified = verify_vk_launch_params(req.launch_params, client_secret)
    if not verified:
        if dev_mode and req.launch_params.startswith("dev:"):
            parts = req.launch_params.split(":")
            uid = int(parts[1]) if len(parts) > 1 else 20001
            verified = {"user_id": uid, "app_id": 1}
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Недействительные параметры запуска VK",
            )

    account = await AccountService.get_or_create_account(
        db,
        platform="vk",
        user_id=verified["user_id"],
    )
    token = await SessionManager.create_session(db, account.id, "vk", verified["user_id"])
    await db.commit()
    return AuthResponse(token=token, platform="vk", user_id=verified["user_id"], account_id=account.id)


@app.post("/api/miniapp/v1/auth/logout")
async def logout(
    token: str = Depends(get_token_from_header),
    db: aiosqlite.Connection = Depends(get_db),
):
    await SessionManager.revoke_session(db, token)
    await db.commit()
    return {"success": True}


# --- Маршруты профиля и настроек ---

def _build_profile_response(account: Account, current_ident: AccountIdentity) -> ProfileResponse:
    identities_dto = [
        IdentityDto(
            platform=i.platform,
            user_id=i.user_id,
            username=i.username,
            full_name=i.full_name,
        )
        for i in account.identities
    ]
    subscriptions_dto = [
        SubscriptionDto(
            id=s.id,
            subscription_type=s.subscription_type,
            subscription_key=s.subscription_key,
            subscription_title=s.subscription_title,
            subscription_url=s.subscription_url,
            group_name=s.group_name,
            schedule_id=s.schedule_id,
            university_subgroup=s.university_subgroup,
            is_primary=s.is_primary,
            notifications_enabled=s.notifications_enabled,
        )
        for s in account.subscriptions
    ]
    return ProfileResponse(
        account_id=account.id,
        current_identity=IdentityDto(
            platform=current_ident.platform,
            user_id=current_ident.user_id,
            username=current_ident.username,
            full_name=current_ident.full_name,
        ),
        identities=identities_dto,
        subscriptions=subscriptions_dto,
        preferences=PreferencesDto(
            theme=account.theme,
            accent=account.accent,
            density=account.density,
            reduced_motion=account.reduced_motion,
            notifications_enabled=account.notifications_enabled,
        ),
    )


@app.get("/api/miniapp/v1/me", response_model=ProfileResponse)
async def get_me(
    user_and_account: tuple[Account, AccountIdentity] = Depends(get_current_user_and_account),
):
    account, current_ident = user_and_account
    return _build_profile_response(account, current_ident)


@app.patch("/api/miniapp/v1/me/preferences", response_model=ProfileResponse)
async def update_preferences(
    req: PreferencesUpdateRequest,
    user_and_account: tuple[Account, AccountIdentity] = Depends(get_current_user_and_account),
    db: aiosqlite.Connection = Depends(get_db),
):
    account, current_ident = user_and_account
    updated = await AccountService.update_preferences(
        db,
        account.id,
        theme=req.theme,
        accent=req.accent,
        density=req.density,
        reduced_motion=req.reduced_motion,
        notifications_enabled=req.notifications_enabled,
    )
    if not updated:
        raise HTTPException(status_code=400, detail="Не удалось обновить настройки")
    await db.commit()
    return _build_profile_response(updated, current_ident)


# --- Поиск групп ---

@app.get("/api/miniapp/v1/groups", response_model=list[GroupItemDto])
async def search_groups(
    query: str = Query("", min_length=1),
    db: aiosqlite.Connection = Depends(get_db),
):
    q = query.strip().lower()
    # 1. Поиск в GroupCatalog если доступен
    catalog_path = Path("group_catalog.json")
    results_map: dict[str, GroupItemDto] = {}
    if catalog_path.exists():
        with suppress(Exception):
            catalog = GroupCatalog(catalog_path)
            for group in catalog.groups:
                if q in group.name.lower():
                    results_map[f"group:{group.schedule_id}"] = GroupItemDto(
                        group_name=group.name,
                        schedule_id=group.schedule_id,
                        source_key=f"group:{group.schedule_id}",
                        source_title=group.name,
                        source_url=f"http://asu.sf-misis.ru/rasp/{group.schedule_id}",
                    )

    # 2. Поиск в БД по таблице groups
    with suppress(Exception):
        cur_g = await db.execute(
            """SELECT group_name, schedule_id FROM groups WHERE group_name IS NOT NULL"""
        )
        for row in await cur_g.fetchall():
            g_name = row[0]
            if g_name and q in g_name.lower():
                s_id = row[1] or 0
                sk = f"group:{s_id}"
                if sk not in results_map:
                    results_map[sk] = GroupItemDto(
                        group_name=g_name,
                        schedule_id=s_id,
                        source_key=sk,
                        source_title=g_name,
                        source_url=f"http://asu.sf-misis.ru/rasp/{s_id}",
                    )

    # 3. Поиск в БД по сохранённым источникам
    cur = await db.execute(
        """SELECT DISTINCT group_name, schedule_id, source_key, source_title, source_url
           FROM schedule_snapshots
           WHERE group_name IS NOT NULL
           ORDER BY id DESC LIMIT 200"""
    )
    for row in await cur.fetchall():
        g_name = row[0]
        if g_name and q in g_name.lower():
            sk = row[2] or f"group:{row[1]}"
            if sk not in results_map:
                results_map[sk] = GroupItemDto(
                    group_name=g_name,
                    schedule_id=row[1] or 0,
                    source_key=sk,
                    source_title=row[3] or g_name,
                    source_url=row[4] or f"rasp:{row[1]}",
                )

    return list(results_map.values())[:30]


# --- Управление подписками ---

@app.post("/api/miniapp/v1/me/subscriptions", response_model=SubscriptionDto)
async def add_subscription(
    req: AddSubscriptionRequest,
    user_and_account: tuple[Account, AccountIdentity] = Depends(get_current_user_and_account),
    db: aiosqlite.Connection = Depends(get_db),
):
    account, _ = user_and_account
    title = req.subscription_title or req.group_name or "Группа"
    s_id = req.schedule_id
    s_key = req.subscription_key or (f"group:{s_id}" if s_id else f"group-pending:{title.lower()}")
    url = req.subscription_url or (f"rasp:{s_id}" if s_id else "")

    sub = await AccountService.add_subscription(
        db,
        account.id,
        subscription_type=req.subscription_type,
        subscription_key=s_key,
        subscription_title=title,
        subscription_url=url,
        group_name=req.group_name or title,
        schedule_id=s_id,
        university_subgroup=req.university_subgroup,
        is_primary=req.is_primary,
        notifications_enabled=req.notifications_enabled,
    )
    await db.commit()
    return SubscriptionDto(
        id=sub.id,
        subscription_type=sub.subscription_type,
        subscription_key=sub.subscription_key,
        subscription_title=sub.subscription_title,
        subscription_url=sub.subscription_url,
        group_name=sub.group_name,
        schedule_id=sub.schedule_id,
        university_subgroup=sub.university_subgroup,
        is_primary=sub.is_primary,
        notifications_enabled=sub.notifications_enabled,
    )


@app.patch("/api/miniapp/v1/me/subscriptions/{sub_id}", response_model=SubscriptionDto)
async def update_subscription(
    sub_id: int,
    req: UpdateSubscriptionRequest,
    user_and_account: tuple[Account, AccountIdentity] = Depends(get_current_user_and_account),
    db: aiosqlite.Connection = Depends(get_db),
):
    account, _ = user_and_account
    updated = await AccountService.update_subscription(
        db,
        account.id,
        sub_id,
        is_primary=req.is_primary,
        university_subgroup=req.university_subgroup,
        notifications_enabled=req.notifications_enabled,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    await db.commit()
    return SubscriptionDto(
        id=updated.id,
        subscription_type=updated.subscription_type,
        subscription_key=updated.subscription_key,
        subscription_title=updated.subscription_title,
        subscription_url=updated.subscription_url,
        group_name=updated.group_name,
        schedule_id=updated.schedule_id,
        university_subgroup=updated.university_subgroup,
        is_primary=updated.is_primary,
        notifications_enabled=updated.notifications_enabled,
    )


@app.delete("/api/miniapp/v1/me/subscriptions/{sub_id}")
async def delete_subscription(
    sub_id: int,
    user_and_account: tuple[Account, AccountIdentity] = Depends(get_current_user_and_account),
    db: aiosqlite.Connection = Depends(get_db),
):
    account, _ = user_and_account
    removed = await AccountService.remove_subscription(db, account.id, sub_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    await db.commit()
    return {"success": True}


# --- Связывание аккаунтов ---

@app.post("/api/miniapp/v1/account-links", response_model=CreateLinkRequestResponse)
async def create_link_request(
    user_and_account: tuple[Account, AccountIdentity] = Depends(get_current_user_and_account),
    db: aiosqlite.Connection = Depends(get_db),
):
    account, current_ident = user_and_account
    raw_token, expires_at = await AccountService.create_link_request(
        db, account.id, current_ident.platform, current_ident.user_id, ttl_seconds=300,
    )
    await db.commit()
    return CreateLinkRequestResponse(token=raw_token, expires_at=expires_at)


@app.post("/api/miniapp/v1/account-links/confirm")
async def confirm_link_request(
    req: ConfirmLinkRequest,
    user_and_account: tuple[Account, AccountIdentity] = Depends(get_current_user_and_account),
    db: aiosqlite.Connection = Depends(get_db),
):
    _, current_ident = user_and_account
    success = await AccountService.confirm_link(
        db,
        req.token,
        target_platform=current_ident.platform,
        target_user_id=current_ident.user_id,
        primary_choice=req.primary_choice,
    )
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Не удалось связать аккаунты (код истёк, неверен или аккаунты совпадают)",
        )
    await db.commit()
    return {"success": True}


@app.delete("/api/miniapp/v1/me/identities/{platform}")
async def unlink_identity(
    platform: str,
    user_and_account: tuple[Account, AccountIdentity] = Depends(get_current_user_and_account),
    db: aiosqlite.Connection = Depends(get_db),
):
    account, _ = user_and_account
    try:
        new_account_id = await AccountService.unlink_identity(db, account.id, platform)
        await db.commit()
        return {"success": True, "new_account_id": new_account_id}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- Вспомогательные функции для расписания ---

def _format_lesson_dtos(diff: DayChanges) -> list[LessonDto]:
    result: list[LessonDto] = []
    for item in diff.lessons:
        change_dto = None
        if item.status == "changed" and item.before:
            change_kind = item.changed_fields[0] if item.changed_fields else "changed"
            change_dto = LessonChangeDto(
                kind=change_kind,
                before={"subject": item.before.subject, "teacher": item.before.teacher, "classroom": item.before.classroom},
                after={"subject": item.lesson.subject, "teacher": item.lesson.teacher, "classroom": item.lesson.classroom},
            )
        elif item.status in ("added", "ambiguous"):
            change_dto = LessonChangeDto(
                kind=item.status,
                after={"subject": item.lesson.subject, "teacher": item.lesson.teacher, "classroom": item.lesson.classroom},
            )

        result.append(
            LessonDto(
                number=item.lesson.number,
                subject=item.lesson.subject,
                teacher=item.lesson.teacher,
                classroom=item.lesson.classroom,
                status=item.status,
                change=change_dto,
            )
        )
    return result


async def _load_day_with_diff(
    db_path: Path,
    db: aiosqlite.Connection,
    source_key: str,
    day_date: date,
    today: date,
    subgroup: int,
) -> DayScheduleDto:
    archive = ScheduleDayArchive(db_path)
    archived = await archive.get_day(source_key, day_date)

    raw_day: DaySchedule | None = None
    origin = "unknown"
    observed_at = None

    if archived:
        raw_day = DaySchedule.from_dict(archived["content"])
        origin = archived["origin"]
        observed_at = archived["observed_at"]
    else:
        # Fallback на актуальный снимок
        db_obj = Database(db_path)
        snapshot_dict = await db_obj.get_latest_snapshot("current", source_key=source_key)
        if snapshot_dict:
            for d in snapshot_dict["content"].get("days", []):
                if d.get("date_iso") == day_date.isoformat():
                    raw_day = DaySchedule.from_dict(d)
                    origin = "snapshot"
                    observed_at = snapshot_dict["fetched_at"]
                    break

    day_name = day_date.strftime("%d.%m.%Y")
    if not raw_day:
        return DayScheduleDto(
            date=day_date.isoformat(),
            day_name=day_name,
            status="unpublished",
            origin="unknown",
            lessons=[],
        )

    # Фильтрация подгруппы
    if subgroup > 0:
        raw_day = filter_day(raw_day, subgroup)

    # Получение опоры для сравнения замен
    reference_data = await archive.get_reference(source_key, day_date, today=today)
    ref_lessons: list[Lesson] | None = None
    if reference_data:
        ref_day = DaySchedule.from_dict(reference_data["content"])
        if subgroup > 0:
            ref_day = filter_day(ref_day, subgroup)
        ref_lessons = ref_day.lessons

    diff = compare_lessons(ref_lessons, raw_day.lessons)
    lesson_dtos = _format_lesson_dtos(diff)
    status_str = "available" if lesson_dtos else "confirmed_empty"

    return DayScheduleDto(
        date=day_date.isoformat(),
        day_name=raw_day.date_label or day_name,
        status=status_str,
        origin=origin,
        observed_at=observed_at,
        lessons=lesson_dtos,
    )


# --- Вкладка 1: Расписание ---

@app.get("/api/miniapp/v1/schedule", response_model=ScheduleResponse)
async def get_schedule(
    subscription_id: int | None = Query(None),
    user_and_account: tuple[Account, AccountIdentity] = Depends(get_current_user_and_account),
    db: aiosqlite.Connection = Depends(get_db),
):
    account, _ = user_and_account
    sub = None
    if subscription_id is not None:
        sub = next((s for s in account.subscriptions if s.id == subscription_id), None)
    else:
        sub = next((s for s in account.subscriptions if s.is_primary), None)
        if not sub and account.subscriptions:
            sub = account.subscriptions[0]
    if not sub:
        raise HTTPException(status_code=404, detail="Подписка не найдена у текущего аккаунта")

    db_path = get_db_path()
    tz = os.getenv("APP_TIMEZONE", "Europe/Moscow")
    today = today_in_timezone(tz)
    dates = schedule_dates(today)

    days_dto: list[DayScheduleDto] = []
    for d in dates:
        day_dto = await _load_day_with_diff(
            db_path, db, sub.subscription_key, d, today, sub.university_subgroup,
        )
        days_dto.append(day_dto)

    return ScheduleResponse(
        subscription_id=sub.id,
        subscription_title=sub.subscription_title or sub.group_name or "Расписание",
        days=days_dto,
    )


# --- Вкладка 2: История ---

@app.get("/api/miniapp/v1/history", response_model=HistoryResponse)
async def get_history(
    subscription_id: int | None = Query(None),
    week_start_param: str | None = Query(None, alias="week_start"),
    user_and_account: tuple[Account, AccountIdentity] = Depends(get_current_user_and_account),
    db: aiosqlite.Connection = Depends(get_db),
):
    account, _ = user_and_account
    sub = None
    if subscription_id is not None:
        sub = next((s for s in account.subscriptions if s.id == subscription_id), None)
    else:
        sub = next((s for s in account.subscriptions if s.is_primary), None)
        if not sub and account.subscriptions:
            sub = account.subscriptions[0]
    if not sub:
        raise HTTPException(status_code=404, detail="Подписка не найдена у текущего аккаунта")

    db_path = get_db_path()
    tz = os.getenv("APP_TIMEZONE", "Europe/Moscow")
    today = today_in_timezone(tz)
    first_bound, last_bound = history_bounds(today)

    if week_start_param:
        try:
            monday = date.fromisoformat(week_start_param)
            if monday.weekday() != 0:
                monday = week_start(monday)
        except ValueError:
            monday = default_history_week(today)
    else:
        monday = default_history_week(today)

    past_days = history_week_days(today, monday)
    archive = ScheduleDayArchive(db_path)

    days_dto: list[DayScheduleDto] = []
    for day_date in past_days:
        archived = await archive.get_day(sub.subscription_key, day_date)
        if archived:
            raw_day = DaySchedule.from_dict(archived["content"])
            if sub.university_subgroup > 0:
                raw_day = filter_day(raw_day, sub.university_subgroup)
            diff = compare_lessons(None, raw_day.lessons)
            status_str = "available" if raw_day.lessons else "confirmed_empty"
            days_dto.append(
                DayScheduleDto(
                    date=day_date.isoformat(),
                    day_name=raw_day.date_label or day_date.strftime("%d.%m.%Y"),
                    status=status_str,
                    origin=archived["origin"],
                    observed_at=archived["observed_at"],
                    lessons=_format_lesson_dtos(diff),
                )
            )
        else:
            days_dto.append(
                DayScheduleDto(
                    date=day_date.isoformat(),
                    day_name=day_date.strftime("%d.%m.%Y"),
                    status="not_archived",
                    origin="unknown",
                    lessons=[],
                )
            )

    has_prev = monday - timedelta(days=7) >= week_start(first_bound)
    has_next = monday + timedelta(days=7) <= week_start(last_bound)

    return HistoryResponse(
        subscription_id=sub.id,
        subscription_title=sub.subscription_title or sub.group_name or "История",
        week_start=monday.isoformat(),
        days=days_dto,
        has_previous_week=has_prev,
        has_next_week=has_next,
        bounds={"start": first_bound.isoformat(), "end": last_bound.isoformat()},
    )
