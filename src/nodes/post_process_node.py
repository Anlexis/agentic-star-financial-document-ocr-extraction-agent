"""AgentCore Platform v1.0"""

# FIN-C2-063 — PostProcessNode
# Outer backbone post_process slot: output gate (credential block + PII
# redaction) + expose the final extraction report as formatted_output and result.
#
# TWO distinct controls on the extraction_report string, applied before the
# result is returned to the caller:
#   1. Credential block. The framework's own credential detector is the floor —
#      the same function the platform output gate calls — plus the extra
#      template patterns below. Delegating rather than re-implementing matters:
#      a value the framework catches and this node misses makes the framework
#      raise INSIDE the node wrapper, and the wrapper then discards this node's
#      whole delta, including the clearing performed on a violation. A narrower
#      local pattern set is therefore a containment bypass, not just a missed
#      finding. On a violation this node returns a sanitised notice for BOTH
#      formatted_output and result, clears every output-bearing state field, and
#      sets status=ERROR.
#   2. PII redaction: mask unambiguous PII value patterns (US SSN, long card
#      numbers, e-mail) that reached the output, so residual personal data
#      cannot leak. This is a real PII output policy — the credential scan alone
#      is NOT a PII gate. Both decisions are audited.
#
# The primary field-level control is upstream: InputValidateNode enforces the
# document-type -> field allowlist and requires caller authorization before any
# sensitive field is extracted (src/policy/pii_policy.py). This output pass is
# defence-in-depth for anything that slipped through.
#
# The domain output gate is a module-level function (_gate_output) called from
# inside execute() — NOT an instance method on the node class, which the
# framework would auto-wrap and which would then fail on the real invoke path.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
import re
from typing import Any, ClassVar, Dict, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event

from src.policy.pii_policy import redact_pii
from src.services.llm_factory import resolve_llm
from src.services.llm_review import render_review, review_result

logger = logging.getLogger(__name__)

# Template patterns ON TOP OF the framework detector — never instead of it.
# These are narrower shapes the framework's set does not cover: publishable and
# account key prefixes, shorter opaque key bodies, short bearer values, and a
# `secret: value` assignment anywhere in the rendered report.
_EXTRA_CREDENTIAL_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"(?:sk|pk|ak)-[A-Za-z0-9]{16,}", "api_key_pattern"),
    (r"Bearer\s+[A-Za-z0-9_\-\.]{8,}", "bearer_token"),
    (
        r"(?:password|passwd|secret|api_key|token|access_key|private_key)\s*[:=]\s*\S{8,}",
        "credential_assignment",
    ),
)

# Every state field that can carry answer text or a payload derived from the
# document. On a violation each of these is cleared in the RETURNED delta —
# omitting a key is not clearing it, because partial deltas merge and the
# previous value survives in state.
_OUTPUT_BEARING_FIELDS: Tuple[str, ...] = (
    "extraction_report",
    "extracted_fields",
    "validation_flags",
    "field_confidence",
)


def _gate_output(content: str) -> Optional[str]:
    """Scan output for disallowed credential/secret patterns.

    Returns the first violation's type name, or None if the output is clean.
    The framework detector runs first so this gate can never be narrower than
    the platform gate that will scan the same values a moment later.

    Module-level function (not a node instance method): the framework auto-wraps
    the gate methods on the node class, so a same-named method would not survive
    the real invoke path.
    """
    findings = detect_credentials(content)
    if findings:
        return str(findings[0]["type"])
    for pattern, name in _EXTRA_CREDENTIAL_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return name
    return None


def _cleared_output_state() -> Dict[str, Any]:
    """Return a delta that blanks every output-bearing field, keys present."""
    cleared: Dict[str, Any] = {field: None for field in _OUTPUT_BEARING_FIELDS}
    cleared["min_confidence_met"] = False
    return cleared


class PostProcessNode(FunctionNode):
    """Apply the domain output gate and expose the final extraction report.

    Outer backbone post_process slot.  Declared ANONYMOUS — trust was
    already enforced at PreProcessNode (VERIFIED_EXTERNAL).

    Input state keys:
        extraction_report:  str   — structured JSON payload from inner OutputFormatNode
        min_confidence_met: bool  — overall confidence-threshold flag

    Output state keys (partial dict):
        formatted_output: str
        result:           str
        status:           str
        error_log:        list[str]  (only on ERROR)
        extraction_report / extracted_fields / validation_flags /
        field_confidence / min_confidence_met — cleared on a violation
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        extraction_report: str = state.get("extraction_report") or ""
        min_confidence_met: bool = bool(state.get("min_confidence_met"))

        # ── Fallback for empty report ─────────────────────────────────────────
        if not extraction_report.strip():
            logger.warning("PostProcessNode: extraction_report is empty — using fallback message")
            extraction_report = (
                "[Financial Document Extraction] No extraction content generated. "
                "Check the agent logs for upstream failures."
            )

        # ── Domain output gate: credential block ──────────────────────────────
        _llm, _ = resolve_llm(None, state)
        _remarks = review_result(
            _llm,
            user_input=str(state.get("user_input") or ""),
            result=extraction_report,
            domain="FIN FinancialDocumentOCRExtractionAgent",
        )
        _review = render_review(_remarks)
        # Remarks are LLM text derived from the caller's raw words, so they pass through the
        # same gate the answer does -- appending after the gate would put unscanned text past
        # it. A tripped review is dropped on its own: withholding a correct answer because an
        # advisory remark quoted an identifier would let the review change the outcome, and
        # the whole design rests on it being unable to.
        if _review and isinstance(extraction_report, str) and not _gate_output(extraction_report + _review):
            extraction_report = extraction_report + _review

        violation = _gate_output(extraction_report)
        if violation:
            logger.error("PostProcessNode: output gate blocked the report — %s", violation)
            emit_trace_event(
                "post_process_output_gate_blocked",
                {"violation": violation},
                state,
            )
            # The notice must be TRUTHY: the framework envelope resolves the
            # caller-visible output as `formatted_output or result`, so an empty
            # or absent notice re-opens the fallback to the un-gated answer.
            # It names the violation CLASS only — never the matched value.
            notice = (
                f"[EXTRACTION OUTPUT WITHHELD: the report contained a disallowed "
                f"pattern ({violation}). Contact the data-security team for the "
                f"original payload.]"
            )
            return {
                **_cleared_output_state(),
                "formatted_output": notice,
                "result": notice,
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PostProcessNode: output gate blocked a credential pattern — {violation}"],
            }

        # ── PII output policy (redact) ────────────────────────────────────────
        # Mask unambiguous PII value patterns that reached the output.
        # Legitimate financial values (account numbers, balances, ISO dates) do
        # not match these patterns and are preserved.
        redacted_report, pii_hits = redact_pii(extraction_report)
        if pii_hits:
            logger.warning("PostProcessNode: PII redaction applied — patterns=%s", pii_hits)
            emit_trace_event(
                "post_process_pii_redacted",
                {"pii_patterns": pii_hits, "redaction_count": len(pii_hits)},
                state,
            )
        extraction_report = redacted_report

        logger.info(
            "PostProcessNode: output gate passed — length=%d min_confidence_met=%s pii_redacted=%s",
            len(extraction_report),
            min_confidence_met,
            bool(pii_hits),
        )
        emit_trace_event(
            "post_process_complete",
            {
                "output_length": len(extraction_report),
                "min_confidence_met": min_confidence_met,
                "pii_redacted": bool(pii_hits),
            },
            state,
        )

        return {
            "formatted_output": extraction_report,
            "result": extraction_report,
            "status": AgentStatus.SUCCESS.value,
        }
