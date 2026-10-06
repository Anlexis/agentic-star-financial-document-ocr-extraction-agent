"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never a Pydantic BaseModel. LangGraph
# checkpoints use msgpack serialization; Pydantic objects cause silent
# corruption. Extend AgentState with agent-specific fields only. Do NOT add
# credentials, secrets, or Pydantic models.
#
# FIN-C2-063 — Financial Document OCR & Extraction Agent
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner
# domain workflow (BaseGraph).  Fields below cover both layers.
#
# Serialization contract: all dict/list-valued fields are stored as JSON-
# serialized Optional[str].  Use to_json() / from_json() below at every
# producer and consumer node — one contract end-to-end. Never type a dict/list
# field as a bare dict/list; that causes msgpack serialization failures.
#
# Raw document bytes must NEVER enter State — only the document_reference
# identifier is carried. OCR text/blocks are derived, schema-scoped, and
# PII-audited before they leave the output gate.

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a value to a JSON string for State storage."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON string from State storage."""
    if value is None:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class State(AgentState):
    """Flat TypedDict for FIN-C2-063.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.

    dict/list fields use JSON-serialized Optional[str].
    formatted_output is NOT re-declared here — it is inherited from AgentState.
    """

    # ------------------------------------------------------------------
    # Outer layer — set by PreProcessNode (pre_process backbone slot)
    # ------------------------------------------------------------------

    # Validated and normalised JSON string of the extraction request.
    # Produced by PreProcessNode; consumed by inner InputValidateNode.
    validated_input: NotRequired[Optional[str]]

    # JSON-serialised channel/request metadata dict (stored as str).
    # Shape: {"source": str, "channel": str, "document_reference": str}
    enriched_context: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Inner layer — domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # Opaque reference ID for the source document (raw bytes never enter
    # State — only this identifier).  e.g. "DOC-2026-000123".
    document_reference: NotRequired[Optional[str]]

    # JSON-serialised field schema requested by the caller.
    # Shape: {field_name: field_type}  e.g. {"account_number": "string",
    #   "statement_date": "date", "closing_balance": "amount"}
    field_schema: NotRequired[Optional[str]]

    # JSON-serialised OCR / document pre-processing result.
    # Shape: {document_type, mime_type, ocr_engine, page_count,
    #   text_blocks (list[str])}.  Produced by ExtractDocumentDataNode.
    extracted_document_data: NotRequired[Optional[str]]

    # JSON-serialised structured extraction — schema-defined fields only.
    # Shape: {field_name: extracted_value}.  Produced by GenerateSectionsNode;
    # no surplus / free-text blocks reach the report.
    extracted_fields: NotRequired[Optional[str]]

    # JSON-serialised per-field validation result.
    # Shape: {field_name: {"valid": bool, "reason": str}}.
    validation_flags: NotRequired[Optional[str]]

    # JSON-serialised per-field confidence scores.
    # Shape: {field_name: float in [0.0, 1.0]}.
    field_confidence: NotRequired[Optional[str]]

    # True if the overall extraction met the min_confidence_threshold (0.80).
    min_confidence_met: NotRequired[Optional[bool]]

    # Overall confidence threshold forwarded from config/config.yaml by the
    # outer GraphNode -> DomainWorkflowGraph._extra_initial_state(). Read by
    # ValidationCheckNode; absent -> the declared default (0.80) applies.
    min_confidence_threshold: NotRequired[Optional[float]]

    # Final structured-extraction JSON document (str, schema-scoped, PII-safe).
    # Assembled by inner OutputFormatNode from extracted_fields +
    # validation_flags + field_confidence.
    extraction_report: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Outer layer — set by PostProcessNode (post_process backbone slot)
    # ------------------------------------------------------------------

    # Primary result surfaced to the caller.
    # Set to the same content as extraction_report once the output gate passes;
    # replaced by the gate's refusal notice on a violation.
    # formatted_output (from AgentState) is also set by PostProcessNode.
    result: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Tracing / audit — framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: NotRequired[Optional[str]]
    correlation_id: NotRequired[Optional[str]]
    # node_history inherited from AgentState
