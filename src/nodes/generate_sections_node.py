"""AgentCore Platform v1.0"""

# FIN-C2-063 — GenerateSectionsNode
# Inner domain node 3: schema-driven structured extraction (the LLM slot).
#
# v1 implementation: deterministic key/value extraction.
# Production wires the real LLM here via config["configurable"]["system_prompt"]
# (rendered from prompts/financial_document_extraction.j2). In v1 the field
# values are extracted from the OCR text blocks using a deterministic
# key:value heuristic — no framework.services.llm_client in SDK v1.
#
# Output is schema-scoped: ONLY fields declared in field_schema are emitted
# (output scope — no surplus free-text blocks leave this node).
#
# Inner node — ANONYMOUS trust.
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)


def _normalise_key(text: str) -> str:
    """Normalise a label/field name for matching: lowercase, alnum-only."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _parse_key_values(text_blocks: List[str]) -> Dict[str, str]:
    """Parse `Label: value` lines from OCR blocks into a normalised-key map.

    Deterministic v1 extraction primitive. Builds a fresh local dict —
    no module-global mutation.
    """
    kv: Dict[str, str] = {}
    for block in text_blocks:
        if ":" not in block:
            continue
        label, _, value = block.partition(":")
        key = _normalise_key(label)
        value = value.strip()
        if key and value:
            kv[key] = value
    return kv


def _extract_field(field_name: str, kv: Dict[str, str]) -> Optional[str]:
    """Return the extracted value for a schema field, or None if not found."""
    target = _normalise_key(field_name)
    if target in kv:
        return kv[target]
    # Loose containment match (e.g. schema "account_number" vs OCR "Account No").
    for key, value in kv.items():
        if target and (target in key or key in target):
            return value
    return None


class GenerateSectionsNode(FunctionNode):
    """Schema-driven structured field extraction (LLM slot) for FIN-C2-063.

    Deterministic key:value extraction from OCR text blocks. A future version
    drives a model from config["configurable"]["system_prompt"].

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        extracted_document_data: str  — JSON OCR result
        field_schema:            str  — JSON schema {field: type}

    Output state keys (partial dict):
        extracted_fields: str  — JSON {field: extracted_value_or_null}
        status:           str
        error_log:        list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        doc_data: Dict[str, Any] = from_json(state.get("extracted_document_data"), {})
        schema: Dict[str, Any] = from_json(state.get("field_schema"), {})

        # No model is invoked here. This node is the slot a model would
        # occupy, but extraction in this version is fully deterministic (the
        # key:value heuristic below): no prompt template is loaded and no
        # request leaves the process. config/config.yaml declares
        # llm.system_prompt_template (prompts/financial_document_extraction.j2)
        # plus temperature/max_tokens, and the outer graph forwards
        # `system_prompt_template` into the inner config, while this node reads
        # configurable["system_prompt"]. Neither value is consumed today; both
        # are staged for the version that wires a real model here (render the
        # .j2 into system_prompt, then call the model), at which point the two
        # keys are reconciled. See docs/02_design.md.
        configurable = (config or {}).get("configurable", {})
        _system_prompt = configurable.get("system_prompt", "")  # noqa: F841 — staged for a future version

        if not schema:
            logger.error("GenerateSectionsNode: field_schema missing in state")
            emit_trace_event(
                "generate_sections_failed",
                {"reason": "missing_field_schema"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["GenerateSectionsNode: field_schema missing in state"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + ("GenerateSectionsNode: field_schema missing in state"),
            }

        text_blocks: List[str] = list(doc_data.get("text_blocks", []))
        kv = _parse_key_values(text_blocks)

        # ── Extract each schema-defined field (schema scope only) ─────────────
        extracted_fields: Dict[str, Optional[str]] = {}
        for field_name in schema.keys():
            extracted_fields[field_name] = _extract_field(field_name, kv)

        found = sum(1 for v in extracted_fields.values() if v is not None)
        logger.info(
            "GenerateSectionsNode: document_reference=%s fields=%d found=%d",
            doc_data.get("document_reference", "unknown"),
            len(extracted_fields),
            found,
        )
        emit_trace_event(
            "generate_sections_complete",
            {
                "document_reference": doc_data.get("document_reference", "unknown"),
                "field_count": len(extracted_fields),
                "found_count": found,
            },
            state,
        )

        return {
            "extracted_fields": to_json(extracted_fields),
            "status": AgentStatus.SUCCESS.value,
        }
