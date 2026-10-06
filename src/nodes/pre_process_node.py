"""AgentCore Platform v1.0"""

# FIN-C2-063 — PreProcessNode
# Outer backbone pre_process slot: trust gate + extraction-request validation.
#
# Responsibilities:
#   - Enforce VERIFIED_EXTERNAL trust (required_trust_level)
#   - Reject empty / non-JSON input early (fail-fast)
#   - Confirm required request fields are present (document_reference,
#     document_type, field_schema) and the MIME type is whitelisted
#   - Bound every caller-controlled structure (schema entry count, text size)
#   - Lock every caller string that renders into the extraction report to an
#     inert identifier alphabet
#   - Refuse chat-template control tokens anywhere in the parsed request,
#     inspecting keys as well as values
#   - Write validated_input (normalised JSON string) + enriched_context to State
#   - Emit an audit event for every validation decision
#
# Raw document bytes must NEVER enter State — only the document_reference
# identifier (and optionally caller-supplied ocr_text for the deterministic
# extraction path) is carried forward.
#
# No rejection message ever echoes a rejected value: errors name the field.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import json
import logging
from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.policy.content_policy import (
    find_control_tokens,
    is_inert_document_reference,
    is_inert_field_name,
    is_inert_field_type,
)
from src.schemas.state import to_json

logger = logging.getLogger(__name__)

# Required top-level keys for a valid extraction request.
_REQUIRED_REQUEST_KEYS = frozenset(
    {
        "document_reference",
        "document_type",
        "field_schema",
    }
)

# Only these MIME types may be submitted for extraction.
_ALLOWED_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/tiff",
    }
)

# Resource guards. The deterministic path is scoped to caller-supplied text
# (production OCR is out of scope — see docs/02), so both the text and the
# requested schema are bounded here: an unbounded schema is an output
# amplifier, since every requested field is rendered into the report whether or
# not it was found.
_MAX_OCR_TEXT_CHARS = 1_000_000
_MAX_SCHEMA_FIELDS = 64


class PreProcessNode(FunctionNode):
    """Caller-request validation for FIN-C2-063.

    Validates the caller-supplied extraction request before the domain
    workflow runs.  This is the outer backbone's pre_process slot — the
    only node requiring VERIFIED_EXTERNAL trust, so unauthenticated or
    anonymous callers are rejected here (fail-fast; inner domain nodes
    carry ANONYMOUS trust and never see untrusted input directly).

    Input state keys:
        user_input: str  — caller-supplied JSON extraction request

    Output state keys (partial dict):
        validated_input:  str        — normalised JSON string (re-serialised)
        enriched_context: str        — JSON-serialised channel metadata
        status:           str        — AgentStatus.SUCCESS or ERROR
        error_log:        list[str]  — set only on ERROR
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _reject(self, state: AgentState, reason: str, message: str, **detail: Any) -> Dict[str, Any]:
        """Return the standard rejection delta and audit the decision.

        `detail` carries locations, counts and closed-set labels only — never a
        rejected value, which is caller data.
        """
        logger.warning("PreProcessNode: request rejected — %s", reason)
        emit_trace_event(
            "pre_process_validation_failed",
            {"reason": reason, **detail},
            state,
        )
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"PreProcessNode: {message}"],
            # The runner surfaces `formatted_output or result` as `output`. A reason left only in
            # error_log reaches no one: the terminal result carries just `status`, and get_output()
            # does not copy error_log out of the graph -- the caller sees a blank spinner.
            "formatted_output": "Request could not be completed. " + (f"PreProcessNode: {message}"),
        }

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})

        # ── Emptiness check ───────────────────────────────────────────────────
        if not user_input or not isinstance(user_input, str) or not user_input.strip():
            return self._reject(state, "empty_input", "user_input is empty or missing")

        # ── JSON parse ────────────────────────────────────────────────────────
        try:
            payload: Dict[str, Any] = json.loads(user_input.strip())
        except (json.JSONDecodeError, ValueError) as exc:
            # exc renders the parse position, never the payload content.
            return self._reject(
                state,
                "json_parse_error",
                f"invalid JSON — {exc}",
                detail=str(exc),
            )

        if not isinstance(payload, dict):
            return self._reject(state, "payload_not_object", "JSON root must be an object")

        # ── OCR-text size guard ───────────────────────────────────────────────
        # Checked first among the field rules: every later pass walks this
        # string, so the bound has to exist before any scanning work is done.
        ocr_text = payload.get("ocr_text", "")
        if not isinstance(ocr_text, str):
            return self._reject(state, "ocr_text_not_a_string", "ocr_text must be a string")
        if len(ocr_text) > _MAX_OCR_TEXT_CHARS:
            return self._reject(
                state,
                "ocr_text_too_large",
                f"ocr_text exceeds the {_MAX_OCR_TEXT_CHARS}-character limit "
                "(this version accepts caller-supplied text only)",
                length=len(ocr_text),
            )

        # ── Chat-template control tokens (keys and values, depth-first) ────────
        # Runs before the field checks so a hostile field NAME is refused as a
        # control token rather than reported as an unknown field.
        found = find_control_tokens(payload)
        if found:
            location, token_class = found
            return self._reject(
                state,
                "control_token_in_request",
                f"request field '{location}' contains a chat-template control "
                f"token ({token_class}); remove it and resubmit",
                location=location,
                token_class=token_class,
            )

        # ── Required field check ──────────────────────────────────────────────
        missing = _REQUIRED_REQUEST_KEYS - payload.keys()
        if missing:
            return self._reject(
                state,
                "missing_required_fields",
                f"missing required fields: {sorted(missing)}",
                missing=sorted(missing),
            )

        # ── document_reference: inert identifier ──────────────────────────────
        # This value is rendered verbatim into the extraction report, so it is
        # locked to an identifier alphabet and a length bound.
        document_reference = payload.get("document_reference")
        if not isinstance(document_reference, str) or not is_inert_document_reference(document_reference):
            return self._reject(
                state,
                "document_reference_not_inert",
                "document_reference must be 1-64 characters of letters, digits, '.', ':', '_' or '-'",
            )

        # ── MIME-type whitelist ───────────────────────────────────────────────
        mime_type = str(payload.get("mime_type", "application/pdf")).lower()
        if mime_type not in _ALLOWED_MIME_TYPES:
            return self._reject(
                state,
                "mime_type_not_allowed",
                f"mime_type not in whitelist {sorted(_ALLOWED_MIME_TYPES)}",
            )

        # ── field_schema: bounded, inert names and types ──────────────────────
        field_schema = payload.get("field_schema")
        if not isinstance(field_schema, dict) or not field_schema:
            return self._reject(
                state,
                "field_schema_invalid",
                "field_schema must be a non-empty object mapping field names to types",
            )
        if len(field_schema) > _MAX_SCHEMA_FIELDS:
            return self._reject(
                state,
                "field_schema_too_large",
                f"field_schema declares more than the {_MAX_SCHEMA_FIELDS}-field limit",
                field_count=len(field_schema),
            )
        for index, (name, declared_type) in enumerate(field_schema.items()):
            if not isinstance(name, str) or not is_inert_field_name(name):
                return self._reject(
                    state,
                    "field_name_not_inert",
                    f"field_schema entry #{index} has a name that is not a plain "
                    "identifier (letters, digits and '_', starting with a letter, "
                    "max 64 characters)",
                    entry_index=index,
                )
            if not isinstance(declared_type, str) or not is_inert_field_type(declared_type.lower()):
                return self._reject(
                    state,
                    "field_type_not_inert",
                    f"field_schema entry '{name}' declares a type that is not a "
                    "plain lowercase identifier (max 32 characters)",
                    entry_index=index,
                )

        # ── Success ───────────────────────────────────────────────────────────
        normalised_json = json.dumps(payload, ensure_ascii=False)

        logger.info(
            "PreProcessNode: validated document_reference=%s mime=%s payload_keys=%d",
            document_reference,
            mime_type,
            len(payload),
        )
        emit_trace_event(
            "pre_process_validated",
            {
                "document_reference": document_reference,
                "mime_type": mime_type,
                "schema_field_count": len(field_schema),
            },
            state,
        )

        return {
            "validated_input": normalised_json,
            "enriched_context": to_json(
                {
                    "source": "FinancialDocumentOCRExtractionAgent",
                    "channel": str(input_context.get("channel", "unknown"))[:64],
                    "document_reference": document_reference,
                }
            ),
            "status": AgentStatus.SUCCESS.value,
        }
