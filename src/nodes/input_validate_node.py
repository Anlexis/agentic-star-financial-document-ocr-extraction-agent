"""AgentCore Platform v1.0"""

# FIN-C2-063 — InputValidateNode
# Inner domain node 1: domain-level validation of the extraction request.
#
# Distinct from PreProcessNode (trust gate + structural JSON check):
# this node applies domain-business rules — document_type whitelist,
# field_schema well-formedness, and document_reference normalisation.
#
# Inner node — ANONYMOUS trust (outer PreProcessNode with VERIFIED_EXTERNAL
# already enforced trust; inner nodes must be ANONYMOUS so the outer
# InvocationContext passes through the GraphNode boundary without rejection;
# InvocationContext passes through the subgraph boundary unchanged).
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

from src.policy.pii_policy import AUTHORIZATION_KEY, authorize_fields
from src.schemas.state import to_json

logger = logging.getLogger(__name__)

# Whitelisted financial document types (extend via config in production).
_ALLOWED_DOCUMENT_TYPES = frozenset(
    {
        "loan_application",
        "kyc_document",
        "bank_statement",
        "trade_confirmation",
        "invoice",
        "other",
    }
)


class InputValidateNode(FunctionNode):
    """Domain validation of the extraction request for FIN-C2-063.

    Applies business-rule checks beyond the structural JSON check in
    PreProcessNode: document_type whitelist, field_schema well-formedness,
    document_reference normalisation.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        validated_input: str  — normalised JSON string from PreProcessNode
                                Falls back to user_input for unit-test convenience.

    Output state keys (partial dict):
        document_reference: str   — normalised document identifier
        field_schema:       str   — JSON-serialised requested field schema
        status:             str
        error_log:          list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raw = state.get("validated_input") or state.get("user_input", "")

        # ── Parse ─────────────────────────────────────────────────────────────
        try:
            payload: Dict[str, Any] = json.loads(raw) if isinstance(raw, str) else {}
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("InputValidateNode: JSON parse error — %s", exc)
            emit_trace_event(
                "input_validate_failed",
                {"reason": "json_parse_error", "detail": str(exc)},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"InputValidateNode: JSON parse error — {exc}"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + (f"InputValidateNode: JSON parse error — {exc}"),
            }

        if not isinstance(payload, dict):
            emit_trace_event(
                "input_validate_failed",
                {"reason": "payload_not_dict"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InputValidateNode: payload is not a JSON object"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + ("InputValidateNode: payload is not a JSON object"),
            }

        # ── document_reference ────────────────────────────────────────────────
        document_reference = str(payload.get("document_reference", "")).strip()
        if not document_reference:
            emit_trace_event(
                "input_validate_failed",
                {"reason": "empty_document_reference"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InputValidateNode: document_reference is missing or empty"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + ("InputValidateNode: document_reference is missing or empty"),
            }

        # ── document_type whitelist ───────────────────────────────────────────
        document_type = str(payload.get("document_type", "")).strip().lower()
        if document_type not in _ALLOWED_DOCUMENT_TYPES:
            emit_trace_event(
                "input_validate_failed",
                {"reason": "document_type_not_allowed", "document_type": document_type},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    f"InputValidateNode: document_type '{document_type}' not in "
                    f"whitelist {sorted(_ALLOWED_DOCUMENT_TYPES)}"
                ],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                # The caller-visible form names the FIELD and the RULE only. The error_log
                # line above keeps the submitted value and the whitelist for operators,
                # where it never leaves the graph; reflecting the submitted value into the
                # response echoes unvalidated input back, and printing the whitelist hands
                # out an internal registry to anyone who guesses one wrong value.
                "formatted_output": "Request could not be completed. "
                + "InputValidateNode: document_type is not one of the accepted document types",
            }

        # ── field_schema well-formedness ──────────────────────────────────────
        field_schema = payload.get("field_schema", {})
        if not isinstance(field_schema, dict) or not field_schema:
            emit_trace_event(
                "input_validate_failed",
                {"reason": "invalid_field_schema"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "InputValidateNode: field_schema must be a non-empty object " "mapping field names to types"
                ],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + ("InputValidateNode: field_schema must be a non-empty object mapping field names to types"),
            }

        # Normalise schema: field-name -> lowercase declared type string.
        normalised_schema: Dict[str, str] = {str(name): str(ftype).lower() for name, ftype in field_schema.items()}

        # ── Output-authorization gate ─────────────────────────────────────────
        # Schema-scoping alone does not stop a caller from naming sensitive
        # financial / identity fields. Enforce the approved document-type ->
        # field allowlist AND require explicit caller authorization/consent
        # (AUTHORIZATION_KEY) before any high-PII field is extracted. Fail-closed
        # on any denial, with an audit record of the decision. This runs BEFORE
        # extraction so unauthorised sensitive values are never produced.
        sensitive_authorized = bool(payload.get(AUTHORIZATION_KEY, False))
        _allowed, denied = authorize_fields(document_type, list(normalised_schema.keys()), sensitive_authorized)
        if denied:
            emit_trace_event(
                "input_validate_authorization_denied",
                {
                    "document_type": document_type,
                    "denied_fields": sorted(denied.keys()),
                    "reasons": denied,
                    "sensitive_authorized": sensitive_authorized,
                },
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "InputValidateNode: extraction not authorized for field(s) "
                    + ", ".join(f"{name} ({reason})" for name, reason in sorted(denied.items()))
                ],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + (
                    "InputValidateNode: extraction not authorized for field(s) "
                    + ", ".join((f"{name} ({reason})" for name, reason in sorted(denied.items())))
                ),
            }

        logger.info(
            "InputValidateNode: document_reference=%s type=%s schema_fields=%d",
            document_reference,
            document_type,
            len(normalised_schema),
        )
        emit_trace_event(
            "input_validate_complete",
            {
                "document_reference": document_reference,
                "document_type": document_type,
                "schema_field_count": len(normalised_schema),
                "sensitive_authorized": sensitive_authorized,
            },
            state,
        )

        return {
            "document_reference": document_reference,
            "field_schema": to_json(normalised_schema),
            "status": AgentStatus.SUCCESS.value,
        }
