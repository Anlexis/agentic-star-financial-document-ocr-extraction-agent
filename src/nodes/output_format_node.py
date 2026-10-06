"""AgentCore Platform v1.0"""

# FIN-C2-063 — OutputFormatNode (inner domain node 5, last in DomainWorkflowGraph)
# Assembles the final structured-extraction JSON payload from extracted_fields,
# validation_flags, and field_confidence.  This is the last inner node — it
# produces the extraction_report string that the outer PostProcessNode gates.
#
# Output scope: the payload contains ONLY schema-defined fields plus their
# validity/confidence metadata — never raw OCR text blocks or surplus content.
#
# Inner node — ANONYMOUS trust.
# Returns ONLY the state keys this node writes (partial-dict contract).

import json
import logging
from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

logger = logging.getLogger(__name__)


def _assemble_payload(
    document_reference: str,
    extracted_fields: Dict[str, Any],
    validation_flags: Dict[str, Any],
    field_confidence: Dict[str, Any],
    min_confidence_met: bool,
) -> Dict[str, Any]:
    """Assemble the schema-scoped structured-extraction payload.

    Emits ONLY schema-defined fields with per-field value/validity/confidence
    (output scope). Builds and returns a fresh local dict — no global mutation.
    """
    fields: Dict[str, Any] = {}
    for field_name, value in extracted_fields.items():
        flag = validation_flags.get(field_name, {})
        fields[field_name] = {
            "value": value,
            "valid": bool(flag.get("valid", False)),
            "reason": flag.get("reason", "unknown"),
            "confidence": field_confidence.get(field_name, 0.0),
        }
    return {
        "document_reference": document_reference,
        "extraction_status": "complete" if min_confidence_met else "low_confidence",
        "min_confidence_met": min_confidence_met,
        "field_count": len(fields),
        "fields": fields,
    }


class OutputFormatNode(FunctionNode):
    """Assemble the final structured-extraction payload (inner domain node).

    Reads extracted_fields, validation_flags, and field_confidence from State,
    renders the schema-scoped JSON extraction payload, and writes it to
    extraction_report (and result) for the outer PostProcessNode.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        extracted_fields:  str  — JSON {field: value}
        validation_flags:  str  — JSON {field: {valid, reason}}
        field_confidence:  str  — JSON {field: float}
        document_reference: str
        min_confidence_met: bool

    Output state keys (partial dict):
        extraction_report: str  — JSON structured-extraction payload
        result:            str  (same as extraction_report — backbone convention)
        status:            str
        error_log:         list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        extracted_fields: Dict[str, Any] = from_json(state.get("extracted_fields"), {})
        validation_flags: Dict[str, Any] = from_json(state.get("validation_flags"), {})
        field_confidence: Dict[str, Any] = from_json(state.get("field_confidence"), {})
        document_reference = str(state.get("document_reference") or "unknown")
        min_confidence_met = bool(state.get("min_confidence_met"))

        if not extracted_fields:
            logger.error(
                "OutputFormatNode: extracted_fields missing for document_reference=%s",
                document_reference,
            )
            emit_trace_event(
                "output_format_failed",
                {"reason": "missing_extracted_fields", "document_reference": document_reference},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    f"OutputFormatNode: extracted_fields missing for " f"document_reference={document_reference}"
                ],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + (f"OutputFormatNode: extracted_fields missing for document_reference={document_reference}"),
            }

        payload = _assemble_payload(
            document_reference,
            extracted_fields,
            validation_flags,
            field_confidence,
            min_confidence_met,
        )
        report = json.dumps(payload, ensure_ascii=False, indent=2)

        logger.info(
            "OutputFormatNode: document_reference=%s report_chars=%d min_confidence_met=%s",
            document_reference,
            len(report),
            min_confidence_met,
        )
        emit_trace_event(
            "output_format_complete",
            {
                "document_reference": document_reference,
                "report_length": len(report),
                "field_count": payload["field_count"],
                "min_confidence_met": min_confidence_met,
            },
            state,
        )

        return {
            "extraction_report": report,
            "result": report,
            "status": AgentStatus.SUCCESS.value,
        }
