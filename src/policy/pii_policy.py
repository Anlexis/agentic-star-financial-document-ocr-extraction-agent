"""AgentCore Platform v1.0"""

# FIN-C2-063 — Output-authorization & PII policy
# Schema-scoping alone does NOT prevent a VERIFIED_EXTERNAL caller from
# requesting or disclosing sensitive financial or identity values, and a
# credential scan is not a PII control. This module supplies the two controls
# that close the gap:
#
#   1. Pre-extraction authorization (consumed by InputValidateNode)
#        - approved document-type -> field allowlist (fixed document types)
#        - name-based sensitivity classification; extracting a high-PII /
#          identity-secret field requires explicit caller authorization/consent
#          (the request-level AUTHORIZATION_KEY flag, on top of the
#          VERIFIED_EXTERNAL trust already enforced by PreProcessNode).
#        Fail-closed: a request that names an off-allowlist field, or a
#        sensitive field without authorization, is rejected before any
#        extraction runs, and the decision is audited by the caller node.
#
#   2. Output PII policy (consumed by PostProcessNode, in addition to — never
#      instead of — the existing credential scan): redact unambiguous PII value
#      patterns (US SSN, long card numbers, e-mail addresses) that reach the
#      formatted output, so residual PII cannot leak even if it slipped into a
#      field value. The decision is audited by the caller node.
#
# Pure module: no I/O, no framework imports, no module-global mutation. All
# lookup tables are immutable (frozenset/tuple) and every helper returns fresh
# local objects — safe to import from multiple nodes.

import re
from typing import Dict, FrozenSet, List, Optional, Tuple

# Request key by which a caller asserts authorization/consent to extract
# high-PII / identity-secret fields.
AUTHORIZATION_KEY = "sensitive_fields_authorized"

# Denial reason codes (stable strings — asserted in tests, logged for audit).
REASON_NOT_APPROVED = "field_not_approved_for_document_type"
REASON_NEEDS_AUTH = "sensitive_field_requires_authorization"

# Approved field allowlist per document type. A requested field outside the
# approved set for a fixed-allowlist document type is rejected before
# extraction. `other` carries no fixed allowlist (None): any NON-sensitive
# field is permitted there, but a sensitive field still requires authorization.
# v1 baseline — tune with the business before regulated production use.
DOCUMENT_TYPE_FIELD_ALLOWLIST: Dict[str, Optional[FrozenSet[str]]] = {
    "loan_application": frozenset(
        {
            "applicant_name",
            "application_date",
            "loan_amount",
            "loan_term",
            "loan_purpose",
            "interest_rate",
            "annual_income",
            "employment_status",
            "collateral_type",
            "property_address",
            "currency",
            # sensitive (require authorization): identity secrets used for KYC on a loan
            "date_of_birth",
            "national_id",
            "tax_id",
            "ssn",
        }
    ),
    "kyc_document": frozenset(
        {
            "full_name",
            "nationality",
            "address",
            "occupation",
            "id_type",
            "id_expiry",
            "issuing_country",
            # sensitive (require authorization)
            "date_of_birth",
            "national_id",
            "passport_number",
            "tax_id",
            "driver_license",
            "ssn",
            "my_number",
        }
    ),
    "bank_statement": frozenset(
        {
            "account_number",
            "account_holder",
            "bank_name",
            "branch",
            "statement_date",
            "period_start",
            "period_end",
            "currency",
            "opening_balance",
            "closing_balance",
            "available_balance",
        }
    ),
    "trade_confirmation": frozenset(
        {
            "trade_id",
            "instrument",
            "direction",
            "quantity",
            "price",
            "notional",
            "currency",
            "trade_date",
            "settlement_date",
            "counterparty",
            "account_number",
        }
    ),
    "invoice": frozenset(
        {
            "invoice_number",
            "invoice_date",
            "due_date",
            "po_number",
            "vendor_name",
            "customer_name",
            "subtotal",
            "tax_amount",
            "total_amount",
            "currency",
        }
    ),
    "other": None,
}

# ── Name-based sensitivity classification ─────────────────────────────────────
# Whole-word markers (matched against split tokens — avoids substring pitfalls
# such as "shipping" containing "pin").
_SENSITIVE_WORDS: FrozenSet[str] = frozenset(
    {
        "ssn",
        "tin",
        "cvv",
        "cvc",
        "pin",
        "dob",
        "passport",
        "password",
        "passwd",
        "secret",
        "biometric",
    }
)
# Multi-word / joined phrases (>= 5 chars, low collision) matched as substrings
# of the alphanumeric-normalised field name.
_SENSITIVE_PHRASES: Tuple[str, ...] = (
    "socialsecurity",
    "dateofbirth",
    "creditcard",
    "debitcard",
    "cardnumber",
    "cardno",
    "taxid",
    "taxpayerid",
    "nationalid",
    "residentid",
    "driverlicense",
    "driverslicense",
    "drivinglicence",
    "passportnumber",
    "securitycode",
    "mynumber",
    "individualnumber",
)


def _words(name: str) -> FrozenSet[str]:
    """Lowercase alphanumeric tokens of a field name (fresh set)."""
    return frozenset(t for t in re.split(r"[^a-z0-9]+", name.lower()) if t)


def _normalize(name: str) -> str:
    """Alphanumeric-only lowercase form of a field name."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def is_sensitive_field(field_name: str) -> bool:
    """True if the field name denotes high-PII / an identity secret."""
    if _words(field_name) & _SENSITIVE_WORDS:
        return True
    norm = _normalize(field_name)
    return any(phrase in norm for phrase in _SENSITIVE_PHRASES)


def authorize_field(field_name: str, document_type: str, sensitive_authorized: bool) -> Optional[str]:
    """Return None if the field may be extracted, else a denial reason code.

    Order: allowlist first (is the field appropriate for this document type),
    then sensitivity/authorization (may this caller extract it).
    """
    allow = DOCUMENT_TYPE_FIELD_ALLOWLIST.get(document_type, None)
    if allow is not None and field_name not in allow:
        return REASON_NOT_APPROVED
    if is_sensitive_field(field_name) and not sensitive_authorized:
        return REASON_NEEDS_AUTH
    return None


def authorize_fields(
    document_type: str, field_names: List[str], sensitive_authorized: bool
) -> Tuple[List[str], Dict[str, str]]:
    """Split requested fields into (allowed, {denied_field: reason}).

    Builds fresh local containers — no shared/global mutation.
    """
    allowed: List[str] = []
    denied: Dict[str, str] = {}
    for name in field_names:
        reason = authorize_field(name, document_type, sensitive_authorized)
        if reason is None:
            allowed.append(name)
        else:
            denied[name] = reason
    return allowed, denied


# ── Output PII policy (redaction) ─────────────────────────────────────────────
# Unambiguous PII value patterns only, so legitimate financial values (account
# numbers, balances, ISO dates) are never masked. Ordered by specificity.
# card_number: 13-19 digits, optionally separated by single spaces/hyphens.
# Requires >= 13 so a 12-digit account number never matches, and rejects any
# comma-grouped value (commas are not in the separator class) so financial
# balances such as "1,250,000.00" are left intact.
_PII_VALUE_PATTERNS: Tuple[Tuple["re.Pattern[str]", str], ...] = (
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "us_ssn"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "card_number"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "email"),
)


def scan_pii(text: str) -> List[str]:
    """Return the labels of unambiguous PII patterns present in `text`."""
    if not text:
        return []
    return [label for pattern, label in _PII_VALUE_PATTERNS if pattern.search(text)]


def redact_pii(text: str) -> Tuple[str, List[str]]:
    """Mask unambiguous PII value patterns in `text`.

    Returns (redacted_text, hit_labels). `text` is returned unchanged when no
    pattern matches. Builds a fresh local list of hits — no global mutation.
    """
    if not text:
        return text, []
    hits: List[str] = []
    redacted = text
    for pattern, label in _PII_VALUE_PATTERNS:
        if pattern.search(redacted):
            hits.append(label)
            redacted = pattern.sub(f"[REDACTED:{label}]", redacted)
    return redacted, hits
