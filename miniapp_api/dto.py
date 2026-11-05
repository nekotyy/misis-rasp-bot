"""Pydantic-схемы (DTO) для API миниаппа Telegram и VK."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AuthTelegramRequest(BaseModel):
    init_data: str


class AuthVkRequest(BaseModel):
    launch_params: str


class AuthResponse(BaseModel):
    token: str
    platform: str
    user_id: int
    account_id: str


class IdentityDto(BaseModel):
    platform: str
    user_id: int
    username: str | None = None
    full_name: str | None = None


class SubscriptionDto(BaseModel):
    id: int
    subscription_type: str
    subscription_key: str
    subscription_title: str | None = None
    subscription_url: str | None = None
    group_name: str | None = None
    schedule_id: int | None = None
    university_subgroup: int = 0
    is_primary: bool = False
    notifications_enabled: bool = True


class PreferencesDto(BaseModel):
    theme: str = "system"
    accent: str = "default"
    density: str = "comfortable"
    reduced_motion: bool = False
    notifications_enabled: bool = True


class ProfileResponse(BaseModel):
    account_id: str
    current_identity: IdentityDto
    identities: list[IdentityDto]
    subscriptions: list[SubscriptionDto]
    preferences: PreferencesDto


class PreferencesUpdateRequest(BaseModel):
    theme: Literal["system", "light", "dark"] | None = None
    accent: str | None = None
    density: Literal["comfortable", "compact"] | None = None
    reduced_motion: bool | None = None
    notifications_enabled: bool | None = None


class AddSubscriptionRequest(BaseModel):
    group_name: str | None = None
    schedule_id: int | None = None
    subscription_key: str | None = None
    subscription_title: str | None = None
    subscription_url: str | None = None
    subscription_type: str = "group"
    university_subgroup: int = 0
    is_primary: bool = False
    notifications_enabled: bool = True


class UpdateSubscriptionRequest(BaseModel):
    is_primary: bool | None = None
    university_subgroup: int | None = None
    notifications_enabled: bool | None = None


class LessonChangeDto(BaseModel):
    kind: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


class LessonDto(BaseModel):
    number: int
    subject: str
    teacher: str
    classroom: str
    time_start: str | None = None
    time_end: str | None = None
    status: str = "unchanged"
    change: LessonChangeDto | None = None


class DayScheduleDto(BaseModel):
    date: str
    day_name: str
    status: Literal["available", "confirmed_empty", "unpublished", "not_archived"]
    origin: str = "unknown"
    stale: bool = False
    observed_at: str | None = None
    lessons: list[LessonDto] = Field(default_factory=list)


class ScheduleResponse(BaseModel):
    subscription_id: int
    subscription_title: str
    days: list[DayScheduleDto]


class HistoryResponse(BaseModel):
    subscription_id: int
    subscription_title: str
    week_start: str
    days: list[DayScheduleDto]
    has_previous_week: bool
    has_next_week: bool
    bounds: dict[str, str]


class CreateLinkRequestResponse(BaseModel):
    token: str
    expires_at: str


class ConfirmLinkRequest(BaseModel):
    token: str
    primary_choice: Literal["initiator", "target"] = "initiator"


class GroupItemDto(BaseModel):
    group_name: str
    schedule_id: int
    source_key: str
    source_title: str
    source_url: str
