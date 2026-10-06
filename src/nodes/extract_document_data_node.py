"""AgentCore Platform v1.0"""

# FIN-C2-063 — ExtractDocumentDataNode
# Inner domain node 2: OCR / document pre-processing.
#
# Responsibilities:
#   - Obtain the document text/blocks for the referenced document
#   - v1: deterministic — use caller-supplied `ocr_text` when present;
#     production wires the real OCR engine (native/tesseract) keyed by
#     document_reference.  Raw document bytes never enter State.
#   - Emit the normalised OCR result (document metadata + text blocks)
#
# Inner node — ANONYMOUS trust.
# Returns ONLY the state keys this node writes (partial-dict contract).

import json
import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json

logger = logging.getLogger(__name__)

# Supported OCR engines (v1 deterministic path treats all as text pass-through).
_SUPPORTED_OCR_ENGINES = frozenset({"native", "tesseract"})


def _split_text_blocks(ocr_text: str) -> List[str]:
    """Split raw OCR text into non-empty logical blocks (one per line).

    Deterministic v1 helper — production OCR emits positioned blocks; here
    we treat each non-blank line as a block so downstream key:value parsing
    can operate.  Builds and returns a fresh local list (no global mutation).
    """
    blocks: List[str] = []
    for line in ocr_text.splitlines():
        stripped = line.strip()
        if stripped:
            blocks.append(stripped)
    return blocks


class ExtractDocumentDataNode(FunctionNode):
    """OCR / document pre-processing for FIN-C2-063.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        validated_input: str  — normalised JSON request (document_reference,
                                document_type, mime_type, ocr_engine, ocr_text)
                                Falls back to user_input for unit-test convenience.

    Output state keys (partial dict):
        extracted_document_data: str  — JSON-serialised OCR result:
            {document_type, mime_type, ocr_engine, page_count, text_blocks}
        status: str
        error_log: list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raw = state.get("validated_input") or state.get("user_input", "")

        try:
            payload: Dict[str, Any] = json.loads(raw) if isinstance(raw, str) else {}
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("ExtractDocumentDataNode: JSON parse error — %s", exc)
            emit_trace_event(
                "extract_document_data_failed",
                {"reason": "json_parse_error", "detail": str(exc)},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"ExtractDocumentDataNode: JSON parse error — {exc}"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + (f"ExtractDocumentDataNode: JSON parse error — {exc}"),
            }

        document_reference = str(state.get("document_reference") or payload.get("document_reference", "unknown"))
        document_type = str(payload.get("document_type", "other")).lower()
        mime_type = str(payload.get("mime_type", "application/pdf")).lower()
        ocr_engine = str(payload.get("ocr_engine", "native")).lower()
        if ocr_engine not in _SUPPORTED_OCR_ENGINES:
            ocr_engine = "native"

        # ── OCR (v1 deterministic — caller-supplied text) ─────────────────────
        ocr_text = str(payload.get("ocr_text", "") or "")
        text_blocks = _split_text_blocks(ocr_text)

        if not text_blocks:
            logger.warning(
                "ExtractDocumentDataNode: no OCR text for document_reference=%s "
                "(v1 deterministic path requires caller-supplied ocr_text)",
                document_reference,
            )
            emit_trace_event(
                "extract_document_data_empty",
                {"document_reference": document_reference, "reason": "no_ocr_text"},
                state,
            )
            # Not a hard error — downstream extraction will yield low confidence.

        extracted_document_data: Dict[str, Any] = {
            "document_reference": document_reference,
            "document_type": document_type,
            "mime_type": mime_type,
            "ocr_engine": ocr_engine,
            "page_count": max(1, ocr_text.count("\f") + 1) if ocr_text else 0,
            "text_blocks": text_blocks,
        }

        logger.info(
            "ExtractDocumentDataNode: document_reference=%s engine=%s blocks=%d",
            document_reference,
            ocr_engine,
            len(text_blocks),
        )
        emit_trace_event(
            "extract_document_data_complete",
            {
                "document_reference": document_reference,
                "ocr_engine": ocr_engine,
                "block_count": len(text_blocks),
            },
            state,
        )

        return {
            "extracted_document_data": to_json(extracted_document_data),
            "status": AgentStatus.SUCCESS.value,
        }
