"""Validated creative references carried by the authenticated peer transport."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CreativeRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["telegram"] = "telegram"
    chat_id: str = Field(min_length=1, max_length=64)
    owner_id: str = Field(min_length=1, max_length=128)
    message_id: int | None = None
    message_thread_id: int | None = None
    route_key: str = Field(min_length=1, max_length=256)


class CreativeAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(gt=0)
    ref: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: str = Field(default="raw", max_length=32)


class CreativePeerContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    project_id: int = Field(gt=0)
    request_id: str = Field(min_length=1, max_length=256)
    route: CreativeRoute
    assets: list[CreativeAsset] = Field(default_factory=list, max_length=32)


def validate_peer_context(value: object) -> dict[str, Any] | None:
    """Only signed, bounded creative refs cross the peer boundary; paths never do."""
    if value is None:
        return None
    result = CreativePeerContext.model_validate(value).model_dump(mode="json")
    if len(json.dumps(result)) > 12_000:
        raise ValueError("peer context is too large")
    return result
