"""Pydantic DTOs for the control API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolInfo(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    sensitive: bool


class ToolListResponse(BaseModel):
    tools: list[ToolInfo]


class ToolCallRequest(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)


class ToolCallResponse(BaseModel):
    # dispatch() always returns a string (JSON for structured tools), passed
    # through verbatim so the app can parse per-tool shapes itself.
    result: str


class ChatRequest(BaseModel):
    text: str


class PairRequest(BaseModel):
    code: str
    device_name: str
    push_endpoint: str | None = None
    device_pubkey: str | None = None


class PairResponse(BaseModel):
    token: str  # returned ONCE; only its hash is stored server-side
    device_id: int


class StatusResponse(BaseModel):
    uptime_s: int
    health: dict[str, str]
    active_provider: str


# --- chats -------------------------------------------------------------------

class Chat(BaseModel):
    pk: int
    platform: str
    chat_id: str
    name: str | None
    kind: str
    is_whitelisted: bool
    auto_reply: bool
    image_recognition: bool
    send_policy: str
    delay_policy: dict[str, Any] | None
    persona_id: int | None
    log_deletes: bool
    capture_media: bool
    last_seen_at: str | None


class ChatPatch(BaseModel):
    """Every field is optional; only those set are applied, each through its own
    tool dispatch so the audit trail matches a Telegram-issued change."""

    is_whitelisted: bool | None = None
    auto_reply: bool | None = None
    image_recognition: bool | None = None
    send_policy: str | None = None
    log_deletes: bool | None = None
    capture_media: bool | None = None
    persona_name: str | None = None
    delay_mode: str | None = None  # none | fixed | random
    delay_min_s: float | None = None
    delay_max_s: float | None = None


class ChatPatchResponse(BaseModel):
    chat: Chat
    # field -> raw tool result, so the client can surface per-field failures
    applied: dict[str, str]


class Message(BaseModel):
    id: int
    msg_id: str
    sender_id: str | None
    sender_name: str | None
    is_from_me: bool
    ts: str
    text: str | None
    media_path: str | None
    edited_text: str | None
    edited_at: str | None
    deleted_at: str | None


# --- config / contacts / schedules / costs / approvals -----------------------

class ConfigResponse(BaseModel):
    # Secret-bearing keys are redacted, matching the settings_get tool.
    settings: dict[str, Any]


class ConfigPut(BaseModel):
    value: Any


class Contact(BaseModel):
    name: str
    phone: str
    source: str


class ContactsResponse(BaseModel):
    contacts: list[Contact]
    entries: int
    unique_numbers: int


class ContactSyncEntry(BaseModel):
    name: str
    phone: str


class ContactSyncRequest(BaseModel):
    contacts: list[ContactSyncEntry]


class ContactSyncResponse(BaseModel):
    submitted: int
    stored: int
    skipped: int


class Schedule(BaseModel):
    id: int
    platform: str
    chat_pk: int
    chat_id: str
    chat_name: str | None
    text: str | None
    due_at: str
    status: str


class CostWindow(BaseModel):
    calls: int
    cost_usd: float
    in_tokens: int
    out_tokens: int


class CostBreakdownRow(BaseModel):
    provider: str
    model: str
    purpose: str
    calls: int
    cost_usd: float


class CostsResponse(BaseModel):
    window: str
    total: CostWindow
    breakdown: list[CostBreakdownRow]


class IntegrationStatus(BaseModel):
    provider: str
    account_label: str | None
    scopes: list[str] | None
    linked_at: str | None
    revoked_at: str | None


class IntegrationStatusList(BaseModel):
    integrations: list[IntegrationStatus]


class TelegramLinkStart(BaseModel):
    code: str
    deep_link: str | None
    expires_in_minutes: int


class TelegramStatus(BaseModel):
    linked: bool
    connected: bool
    tg_username: str | None = None
    tg_name: str | None = None
    linked_at: str | None = None
    connected_at: str | None = None


class WhatsAppConsent(BaseModel):
    """The warning the app MUST show before offering to link."""

    version: str
    warning: str
    risk: str
    reversible: bool


class WhatsAppLinkRequest(BaseModel):
    # Deliberately not defaulted to True anywhere: the caller has to say it.
    consent_acknowledged: bool = False
    consent_version: str | None = None


class WhatsAppStatus(BaseModel):
    linked: bool
    status: str
    phone: str | None = None
    consent_version: str | None = None
    consent_acknowledged_at: str | None = None
    paired_at: str | None = None
    last_error: str | None = None


class IntegrationLinkStart(BaseModel):
    authorize_url: str
    state: str


class Approval(BaseModel):
    id: int
    kind: str
    payload: dict[str, Any]
    chat_pk: int | None
    status: str
    created_at: str
    expires_at: str


class ApprovalDecision(BaseModel):
    ok: bool


class ApprovalDecisionResponse(BaseModel):
    status: str  # approved | rejected | expired | already | unknown
    detail: str
    ok: bool
