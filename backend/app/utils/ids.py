from __future__ import annotations

import uuid


def new_id() -> str:
    """Short, URL-friendly identifier used as the primary key of every entity."""
    return uuid.uuid4().hex[:12]
