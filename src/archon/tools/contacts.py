"""Contact-directory tools: resolve a name (in any language) to a phone number,
teach aliases, import a contacts CSV."""

from __future__ import annotations

import json

from .. import contacts as directory
from ..db import repo
from .registry import Registry, ToolContext


def register(registry: Registry) -> None:
    @registry.tool(
        "contact_search",
        "Resolve a person's phone number by name from the owner's contacts. "
        "Names may be Hebrew, Russian, English, etc. Use the returned phone with "
        "wa_send_message (as '<digits>@s.whatsapp.net') or tg_send_private (pass "
        "the phone directly). If you get NO match, the contact is likely saved in "
        "a different script than the query — retry with the name transliterated "
        "(e.g. Russian 'Alex' -> Latin 'Amitai' or Hebrew 'עמיתי'). After "
        "resolving someone the owner named in another language, offer to "
        "contact_remember that alias.",
        {"type": "object", "properties": {"query": {"type": "string"}},
         "required": ["query"]},
        scopes=("owner", "inbound"),
    )
    async def contact_search(ctx: ToolContext, query: str) -> str:
        matches = directory.search(ctx.rt.db, query)
        return json.dumps({"query": query, "matches": matches}, ensure_ascii=False)

    @registry.tool(
        "contact_remember",
        "Save an alias: name -> phone, so this person is found by that name in "
        "future (in whatever language the owner prefers). E.g. remember "
        "'Alex' = +972555000004.",
        {"type": "object",
         "properties": {"name": {"type": "string"}, "phone": {"type": "string"}},
         "required": ["name", "phone"]},
        scopes=("owner",), sensitive=True,
    )
    async def contact_remember(ctx: ToolContext, name: str, phone: str) -> str:
        directory.remember(ctx.rt.db, name, phone)
        return json.dumps({"ok": True, "remembered": {name: directory.normalize_phone(phone)}},
                          ensure_ascii=False)

    @registry.tool(
        "contacts_import",
        "Import contacts from a Google Contacts CSV file already on the server "
        "(path). Normally the owner just sends the .csv to this bot and it "
        "imports automatically; use this only for a known server path.",
        {"type": "object", "properties": {"path": {"type": "string"}},
         "required": ["path"]},
        scopes=("owner",), sensitive=True,
    )
    async def contacts_import(ctx: ToolContext, path: str) -> str:
        try:
            with open(path, encoding="utf-8-sig") as fh:
                text = fh.read()
        except OSError as exc:
            return json.dumps({"error": f"cannot read {path}: {exc}"})
        n = directory.import_csv(ctx.rt.db, text)
        return json.dumps({"imported": n})

    @registry.tool(
        "contacts_stats",
        "How many contacts / unique numbers are in the directory.",
        {"type": "object", "properties": {}},
        scopes=("owner",),
    )
    async def contacts_stats(ctx: ToolContext) -> str:
        row = repo.contact_counts(ctx.rt.db)
        return json.dumps({"entries": row["entries"],
                           "unique_numbers": row["unique_numbers"]})
