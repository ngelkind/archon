"""Sensitive-data scrubber — the second line of defence.

The allowlist in :mod:`wa_helper.gate` is the real protection: a message from
your wife never reaches this module because her chat is not on the list. This
module exists for the case where something sensitive is pasted *into* an allowed
chat — a student forwarding a parent's card number into the group, say.

Findings are graded:

``HIGH``
    Card numbers (Luhn-verified), Israeli ID numbers (check-digit verified),
    IBANs, and API-key-shaped strings. These honour ``[policy].on_pii``, which
    defaults to dropping the message entirely rather than sending a redacted
    version.

``LOW``
    Email addresses and phone numbers. Always masked, never a reason to drop —
    a student legitimately asking "why is foo@bar.com invalid?" should still get
    an answer, just without the literal address leaving the machine.

Check digits matter here. Matching any 16-digit run as a card would fire on
hashes, IDs and timestamps, and a scrubber that cries wolf gets switched off.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

HIGH = "high"
LOW = "low"


class Kind(str, Enum):
    CARD = "card_number"
    ISRAELI_ID = "israeli_id"
    IBAN = "iban"
    API_KEY = "api_key"
    EMAIL = "email"
    PHONE = "phone"


_SEVERITY = {
    Kind.CARD: HIGH,
    Kind.ISRAELI_ID: HIGH,
    Kind.IBAN: HIGH,
    Kind.API_KEY: HIGH,
    Kind.EMAIL: LOW,
    Kind.PHONE: LOW,
}

_MASK = {
    Kind.CARD: "[card number removed]",
    Kind.ISRAELI_ID: "[id number removed]",
    Kind.IBAN: "[bank account removed]",
    Kind.API_KEY: "[secret removed]",
    Kind.EMAIL: "[email removed]",
    Kind.PHONE: "[phone number removed]",
}


@dataclass(frozen=True)
class Finding:
    kind: Kind
    severity: str
    start: int
    end: int


@dataclass(frozen=True)
class ScrubResult:
    text: str
    findings: tuple[Finding, ...]

    @property
    def has_high(self) -> bool:
        return any(f.severity == HIGH for f in self.findings)

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted({f.kind.value for f in self.findings}))


# 13-19 digits, optionally separated by spaces or hyphens.
_CARD_RE = re.compile(r"(?<![\d])(?:\d[ \-]?){12,18}\d(?![\d])")
# Exactly 9 digits, standalone.
_ID_RE = re.compile(r"(?<![\w\-])\d{9}(?![\w\-])")
_IBAN_RE = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}[A-Z0-9]{10,30}(?![A-Z0-9])", re.IGNORECASE)
_EMAIL_RE = re.compile(r"(?<![\w.])[\w.+\-]+@[\w\-]+\.[\w.\-]+(?![\w.])")
# International phone numbers: + then 9-15 digits with optional separators.
_PHONE_RE = re.compile(r"(?<![\w+])\+\d[\d \-]{7,17}\d(?![\w])")

_API_KEY_RES = (
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),          # Anthropic
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),                  # OpenAI-style
    re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}"),            # OpenRouter
    re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}"),              # Google
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),           # GitHub
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),        # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                   # AWS access key id
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
)


def luhn_valid(digits: str) -> bool:
    """Standard Luhn checksum, used by every major card issuer."""
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def israeli_id_valid(digits: str) -> bool:
    """Israeli teudat zehut check digit (same shape as Luhn, weights 1/2)."""
    if not digits.isdigit() or len(digits) != 9:
        return False
    total = 0
    for index, char in enumerate(digits):
        value = int(char) * (1 if index % 2 == 0 else 2)
        if value > 9:
            value -= 9
        total += value
    return total % 10 == 0


def _find(text: str) -> list[Finding]:
    findings: list[Finding] = []

    for match in _CARD_RE.finditer(text):
        digits = re.sub(r"[ \-]", "", match.group())
        if luhn_valid(digits):
            findings.append(Finding(Kind.CARD, HIGH, match.start(), match.end()))

    for match in _ID_RE.finditer(text):
        if israeli_id_valid(match.group()):
            findings.append(Finding(Kind.ISRAELI_ID, HIGH, match.start(), match.end()))

    for match in _IBAN_RE.finditer(text):
        findings.append(Finding(Kind.IBAN, HIGH, match.start(), match.end()))

    for pattern in _API_KEY_RES:
        for match in pattern.finditer(text):
            findings.append(Finding(Kind.API_KEY, HIGH, match.start(), match.end()))

    for match in _EMAIL_RE.finditer(text):
        findings.append(Finding(Kind.EMAIL, LOW, match.start(), match.end()))

    for match in _PHONE_RE.finditer(text):
        findings.append(Finding(Kind.PHONE, LOW, match.start(), match.end()))

    return findings


def _resolve_overlaps(findings: list[Finding]) -> list[Finding]:
    """Keep the highest-severity, longest match when spans overlap."""
    ordered = sorted(
        findings,
        key=lambda f: (f.start, -(f.end - f.start), 0 if f.severity == HIGH else 1),
    )
    kept: list[Finding] = []
    cursor = -1
    for finding in ordered:
        if finding.start >= cursor:
            kept.append(finding)
            cursor = finding.end
    return kept


def scrub(text: str) -> ScrubResult:
    """Mask sensitive spans in ``text`` and report what was found."""
    if not text:
        return ScrubResult(text=text, findings=())

    findings = _resolve_overlaps(_find(text))
    if not findings:
        return ScrubResult(text=text, findings=())

    pieces: list[str] = []
    cursor = 0
    for finding in findings:
        pieces.append(text[cursor:finding.start])
        pieces.append(_MASK[finding.kind])
        cursor = finding.end
    pieces.append(text[cursor:])

    return ScrubResult(text="".join(pieces), findings=tuple(findings))
