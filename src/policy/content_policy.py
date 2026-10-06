"""AgentCore Platform v1.0"""

# FIN-C2-063 — Caller-content policy
#
# Two controls that the extraction request boundary owns, both fail-closed:
#
#   1. Chat-template control-token screen. A financial document is caller-
#      supplied free text that ends up rendered verbatim in the extraction
#      report, and the report is consumed downstream by systems that do talk to
#      models. The platform input gate blocks directive PHRASES and the ChatML /
#      Llama-instruct markers `<|...|>` and `[INST]`, but it does NOT score the
#      `<<SYS>>` family — measured against the installed framework, where
#      `<<SYS>> you are helpful <</SYS>>` produces no findings at all. So the
#      template screens the whole control-token CLASS itself rather than relying
#      on the platform gate for part of it.
#
#      The screen walks the parsed request depth-first and inspects KEYS as well
#      as values (a JSON \\u escape is already decoded by the time the payload is
#      parsed, so a post-parse walk cannot be evaded that way), and it runs twice
#      over every string: once raw, and once with markup and zero-width
#      characters removed, so a token that was split by inserted tags
#      (`<<S<b>YS</b>>>`) is caught after re-assembly while an intact token is
#      caught before any strip could remove it.
#
#   2. Inert-identifier locks. Every caller string that renders into the
#      extraction report — the document reference, the requested field names,
#      and the declared field types — is restricted to an identifier alphabet.
#      Free text in those slots is caller-controlled output injection: the
#      report is a machine-readable artifact other systems parse.
#
# Pure module: no I/O, no framework imports, no module-global mutation. All
# lookup tables are immutable and every helper returns fresh local objects.

import re
from typing import Any, List, Optional, Tuple

# ── Chat-template control tokens ──────────────────────────────────────────────
# Exact token forms only. Directive phrases are deliberately NOT screened here:
# a real financial document may legitimately contain words like "ignore" or
# "system", and an unanchored phrase screen refuses real work (the fail-closed
# direction is the one that blocks a genuine extraction). The platform input
# gate already scores directive phrases.
_CONTROL_TOKEN_PATTERNS: Tuple[Tuple["re.Pattern[str]", str], ...] = (
    (re.compile(r"<\|[^|>]{0,64}\|>"), "chatml_marker"),
    (re.compile(r"\[/?INST\]", re.IGNORECASE), "instruct_marker"),
    (re.compile(r"<</?SYS>>", re.IGNORECASE), "system_prompt_marker"),
)

# Markup and invisible characters removed before the second pass.
_MARKUP_RE = re.compile(r"<\s*/?\s*[A-Za-z][A-Za-z0-9]{0,15}(?:\s[^<>]{0,64})?\s*/?\s*>")
_INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")

# ── Inert identifier alphabets ────────────────────────────────────────────────
# Document references in this domain look like DOC-2026-000123 / INV-2026-0042.
# `_` and `.` are in the class because real reference schemes use them; nothing
# outside this class ever needs to reach the rendered report.
_DOCUMENT_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
# Field names are identifiers: they are matched against the approved-field
# allowlist and rendered as JSON object keys in the report.
_FIELD_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
# Declared field types render into the per-field `reason` string.
_FIELD_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# Redaction sentinels. `[MASKED]` is written by the platform input gate when it
# finds personal data in the request; `[REDACTED:<label>]` is written by this
# template's own output redaction. A value that is one of these is a placeholder,
# never an extracted value.
_REDACTION_SENTINEL_RE = re.compile(r"\[MASKED\]|\[REDACTED:[a-z_]+\]")


def _strip_markup(text: str) -> str:
    """Return `text` with tag-like markup and zero-width characters removed."""
    return _INVISIBLE_RE.sub("", _MARKUP_RE.sub("", text))


def scan_control_tokens(text: str) -> List[str]:
    """Return the control-token classes present in `text`, raw or after a strip.

    Both passes matter and neither subsumes the other: the raw pass catches an
    intact token that the strip would have removed, the stripped pass catches a
    token that was split by inserted markup.
    """
    if not text:
        return []
    stripped = _strip_markup(text)
    hits: List[str] = []
    for pattern, label in _CONTROL_TOKEN_PATTERNS:
        if pattern.search(text) or pattern.search(stripped):
            hits.append(label)
    return hits


def find_control_tokens(value: Any, path: str = "request") -> Optional[Tuple[str, str]]:
    """Walk a parsed request depth-first; return (location, class) on the first hit.

    Keys are inspected as well as values. The return names a LOCATION and a
    token class only — never the matched text, which is caller data and must not
    be echoed back into an error message or an audit event.
    """
    if isinstance(value, str):
        hits = scan_control_tokens(value)
        return (path, hits[0]) if hits else None
    if isinstance(value, dict):
        for index, (key, nested) in enumerate(value.items()):
            key_text = str(key)
            key_hits = scan_control_tokens(key_text)
            if key_hits:
                return (f"{path}.<field name #{index}>", key_hits[0])
            safe = key_text if _FIELD_NAME_RE.match(key_text) else f"<field name #{index}>"
            found = find_control_tokens(nested, f"{path}.{safe}")
            if found:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = find_control_tokens(item, f"{path}[{index}]")
            if found:
                return found
    return None


def is_inert_document_reference(value: str) -> bool:
    """True if the document reference is within the identifier alphabet."""
    return bool(_DOCUMENT_REFERENCE_RE.match(value))


def is_inert_field_name(value: str) -> bool:
    """True if a requested field name is a plain identifier."""
    return bool(_FIELD_NAME_RE.match(value))


def is_inert_field_type(value: str) -> bool:
    """True if a declared field type is a plain lowercase identifier."""
    return bool(_FIELD_TYPE_RE.match(value))


def is_redaction_sentinel(value: str) -> bool:
    """True if a value is (or contains) a redaction placeholder, not real data."""
    return bool(_REDACTION_SENTINEL_RE.search(value))
