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
