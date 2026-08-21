"""Contact directory: name <-> phone, with cross-script name matching.

Names arrive in Hebrew, Russian, English, emoji… and the owner refers to people
in whatever language they like. Matching therefore normalises: Cyrillic is
transliterated to Latin and accents are folded, so "Alex" / "Amitai" collapse
to the same key. Hebrew doesn't transliterate cleanly, so Hebrew characters are
kept verbatim — the agent bridges the remaining script gap by retrying with a
transliterated query, and the owner can teach aliases in any language
(``remember``) which then match directly forever.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from difflib import SequenceMatcher

from .db import Db
from .db import repo
from .db.tenancy import TenantScope

#: A raw Db (single-user -> owner tenant) or a tenant-bound scope.
Store = Db | TenantScope

# Cyrillic -> Latin (lowercase). Values may be multi-character.
_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}
_CYR_TABLE = str.maketrans(_CYR)


def _fold(s: str) -> str:
    s = (s or "").lower().translate(_CYR_TABLE)
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def normalize(name: str) -> str:
    """Compact match key: transliterated Latin + digits, Hebrew kept verbatim."""
    return re.sub(r"[^a-z0-9֐-׿]+", "", _fold(name))


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"[^\d]", "", raw or "")
    return "+" + digits if digits else ""


def import_csv(db: Store, text: str) -> int:
    """Import a Google Contacts CSV export. Returns the number of rows added."""
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    added = 0
    for row in reader:
        name = " ".join(
            p for p in (
                (row.get("First Name") or "").strip(),
                (row.get("Middle Name") or "").strip(),
                (row.get("Last Name") or "").strip(),
            ) if p
        ).strip()
        if not name:
            name = (row.get("Nickname") or row.get("File As")
                    or row.get("Organization Name") or "").strip()
        phones: list[str] = []
        for i in range(1, 6):
            raw = (row.get(f"Phone {i} - Value") or "").strip()
            for part in re.split(r"[;/]| ::: ", raw):
                p = normalize_phone(part)
                if len(p) >= 9 and p not in phones:
                    phones.append(p)
        # Skip junk names (single punctuation, zero-width marks, empties).
        if not phones or not re.search(r"[\w֐-׿Ѐ-ӿ]", name):
            continue
        for phone in phones:
            added += repo.contact_add(db, name, phone, normalize(name), "import")
    return added


def remember(db: Store, name: str, phone: str) -> None:
    phone = normalize_phone(phone)
    repo.contact_add(db, name, phone, normalize(name), "alias")


def search(db: Store, query: str, limit: int = 12) -> list[dict]:
    q = (query or "").strip()
    qn = normalize(q)
    ql = q.lower()
    scored: list[tuple[float, str, str, str]] = []
    for r in repo.contact_rows(db):
        name, phone, norm, source = r["name"], r["phone"], r["norm"], r["source"]
        if qn and norm and qn == norm:
            score = 1.0
        elif qn and norm and (qn in norm or norm in qn):
            score = 0.88
        elif ql and ql in name.lower():
            score = 0.8
        elif qn and norm:
            score = SequenceMatcher(None, qn, norm).ratio()
        else:
            score = 0.0
        if source == "alias":
            score += 0.05  # taught aliases win ties
        if score >= 0.55:
            scored.append((score, name, phone, source))
    scored.sort(key=lambda x: -x[0])
    out: list[dict] = []
    seen: set[str] = set()
    for score, name, phone, _ in scored:
        if phone in seen:
            continue
        seen.add(phone)
        out.append({"name": name, "phone": phone, "score": round(min(score, 1.0), 2)})
        if len(out) >= limit:
            break
    return out
