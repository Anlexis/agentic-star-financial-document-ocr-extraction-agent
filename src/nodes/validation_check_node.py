"""AgentCore Platform v1.0"""

# FIN-C2-063 — ValidationCheckNode
# Inner domain node 4: apply validation rules + per-field confidence scoring.
#
# Responsibilities:
#   - For each schema field: presence + basic format check (amount / date /
#     string) → per-field validity flag
#   - Recognise a redaction placeholder as NOT an extracted value
#   - Compute a per-field confidence score in [0.0, 1.0]
#   - Set min_confidence_met = (min score over the schema fields) >= threshold
#     (min_confidence_threshold, default 0.80 — from config)
#
# Inner node — ANONYMOUS trust.
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
import math
import re
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.policy.content_policy import is_redaction_sentinel
from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

# Default overall min-confidence threshold.
_DEFAULT_MIN_CONFIDENCE = 0.80

# Lightweight format validators keyed by declared schema type.
_AMOUNT_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?$")
_DATE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$|^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}$")

# Stable per-field reason codes (asserted in tests, logged for audit).
REASON_OK = "ok"
REASON_MISSING = "missing"
REASON_REDACTED = "value_redacted_before_extraction"


def _format_valid(field_type: str, value: str) -> bool:
    """Return True if `value` matches the basic format for `field_type`."""
    v = value.strip()
    if not v:
        return False
    if field_type == "amount":
        return bool(_AMOUNT_RE.match(v.replace(" ", "")))
    if field_type == "date":
        return bool(_DATE_RE.match(v))
    # "string" and unknown types: any non-empty value is acceptable.
    return True


def _score_field(present: bool, format_ok: bool) -> float:
    """Confidence score for one field from presence + format validity."""
    if present and format_ok:
        return 0.95
    if present and not format_ok:
        return 0.55
    return 0.0


def resolve_threshold(raw: Any) -> Optional[float]:
    """Return `raw` as a finite confidence threshold in [0.0, 1.0], else None.

    Rejects bools, non-numerics, NaN and ±Infinity. A NaN threshold parses
    through a bare ``float()`` and then compares False against every score, so
    the gate would report "not met" for a perfect extraction and never say why.
    Out-of-range values are equally meaningless. Both degrade to the declared
    default rather than to an unusable comparison.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or not (0.0 <= parsed <= 1.0):
        return None
    return parsed


class ValidationCheckNode(FunctionNode):
    """Validation rules + confidence scoring for FIN-C2-063.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        extracted_fields: str  — JSON {field: value_or_null}
        field_schema:     str  — JSON {field: type}

    Output state keys (partial dict):
        validation_flags:   str   — JSON {field: {valid, reason}}
        field_confidence:   str   — JSON {field: float}
        min_confidence_met: bool
        status:             str
        error_log:          list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        extracted_fields: Dict[str, Any] = from_json(state.get("extracted_fields"), {})
        schema: Dict[str, Any] = from_json(state.get("field_schema"), {})

        if not schema:
            logger.error("ValidationCheckNode: field_schema missing in state")
            emit_trace_event(
                "validation_check_failed",
                {"reason": "missing_field_schema"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidationCheckNode: field_schema missing in state"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + ("ValidationCheckNode: field_schema missing in state"),
            }

        # Resolve the confidence threshold. Precedence:
        #   1. runtime config["configurable"]["min_confidence_threshold"]
        #      — the direct/unit-invocation path (the SDK does not populate this
        #      on the real .invoke() path);
        #   2. state["min_confidence_threshold"] — the declared value forwarded
        #      by the outer GraphNode and seeded into inner state by
        #      DomainWorkflowGraph._extra_initial_state() (the real invoke path);
        #   3. the declared default (0.80).
        configurable = (config or {}).get("configurable", {})
        raw_threshold = configurable.get("min_confidence_threshold")
        if raw_threshold is None:
            raw_threshold = state.get("min_confidence_threshold")
        resolved = resolve_threshold(raw_threshold)
        threshold = _DEFAULT_MIN_CONFIDENCE if resolved is None else resolved

        # ── Per-field validation + confidence (local accumulators only) ───────
        redacted_fields: List[str] = []
        validation_flags: Dict[str, Dict[str, Any]] = {}
        field_confidence: Dict[str, float] = {}
        for field_name, field_type in schema.items():
            value = extracted_fields.get(field_name)
            present = value is not None and str(value).strip() != ""

            # A value the platform input gate replaced with a redaction
            # placeholder is not an extracted value. Reporting the placeholder
            # as a high-confidence valid extraction would tell the caller a
            # bank account number or a person's name was read from the document
            # when what came back is a mask. Score it as not extracted, and say
            # so in the reason — this is the fail-closed direction.
            redacted = present and is_redaction_sentinel(str(value))
            if redacted:
                redacted_fields.append(field_name)
                validation_flags[field_name] = {"valid": False, "reason": REASON_REDACTED}
                field_confidence[field_name] = 0.0
                continue

            format_ok = _format_valid(str(field_type).lower(), str(value)) if present else False
            score = _score_field(present, format_ok)

            if not present:
                reason = REASON_MISSING
            elif not format_ok:
                reason = f"format_invalid_for_type_{field_type}"
            else:
                reason = REASON_OK

            validation_flags[field_name] = {"valid": present and format_ok, "reason": reason}
            field_confidence[field_name] = score

        overall_min = min(field_confidence.values()) if field_confidence else 0.0
        min_confidence_met = overall_min >= threshold

        logger.info(
            "ValidationCheckNode: fields=%d redacted=%d overall_min=%.2f threshold=%.2f met=%s",
            len(field_confidence),
            len(redacted_fields),
            overall_min,
            threshold,
            min_confidence_met,
        )
        emit_trace_event(
            "validation_check_complete",
            {
                "field_count": len(field_confidence),
                "redacted_field_count": len(redacted_fields),
                "overall_min_confidence": overall_min,
                "threshold": threshold,
                "min_confidence_met": min_confidence_met,
            },
            state,
        )

        return {
            "validation_flags": to_json(validation_flags),
            "field_confidence": to_json(field_confidence),
            "min_confidence_met": min_confidence_met,
            "status": AgentStatus.SUCCESS.value,
        }
