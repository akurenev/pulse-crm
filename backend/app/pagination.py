from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, status


@dataclass(frozen=True, slots=True)
class Cursor:
    created_at: datetime
    entity_id: uuid.UUID


def encode_cursor(created_at: datetime, entity_id: uuid.UUID) -> str:
    raw = json.dumps({"created_at": created_at.isoformat(), "id": str(entity_id)}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str | None) -> Cursor | None:
    if value is None:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded).decode())
        return Cursor(
            created_at=datetime.fromisoformat(data["created_at"]), entity_id=uuid.UUID(data["id"])
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid cursor"
        ) from exc
