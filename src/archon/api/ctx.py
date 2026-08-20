"""Build a ToolContext for API-originated calls.

Reuses the SAME control-chat pk that ``agent.owner`` uses, so the app and the
Telegram control bot share one memory thread (the shared "mind"). Scope is
always ``owner`` — a paired device is another key to the single owner identity.
"""

from __future__ import annotations

from ..db import repo
from ..runtime import Runtime
from ..tools.registry import ToolContext


def api_owner_ctx(rt: Runtime) -> ToolContext:
    chat_pk = repo.chat_upsert(
        rt.db, "tg", str(rt.settings.telegram_owner_id), "Archon control", "private"
    )
    return ToolContext(
        rt=rt, scope="owner", origin_chat_pk=chat_pk, extras={"transport": "api"}
    )
