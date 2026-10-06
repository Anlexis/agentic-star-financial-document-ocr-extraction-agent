"""AgentCore Platform v1.0"""

# FIN-C2-063 — DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph for the Cat 2 two-layer nested architecture.
# It encapsulates the financial-document OCR & extraction pipeline:
#
#   START
#     → input_validate           (InputValidateNode)
#     → extract_document_data    (ExtractDocumentDataNode — OCR / parse)
#     → generate_sections        (GenerateSectionsNode — schema extraction / LLM)
#     → validation_check         (ValidationCheckNode — rules + confidence)
#     → output_format            (OutputFormatNode)
#     → END
#
# Called by DocumentExtractionGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   ✅ Inherits BaseGraph (fully custom topology — no forced backbone)
#   ✅ Implements all 7 BaseGraph ABC methods
#   ✅ register_nodes() does NOT call super() (abstract in BaseGraph)
#   ✅ Does NOT register initialize / finalize (outer backbone concerns)
#   ✅ All inner nodes declare required_trust_level = TrustLevel.ANONYMOUS
#   ✅ get_output() designed together with DocumentExtractionGraphNode.merge_output()
#   ✅ All inner node ctors are empty-parens (no constructor args)
#   ❌ No platform SDK imports
#   ❌ Not placed under src/subagents/

from typing import Any, Dict

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.extract_document_data_node import ExtractDocumentDataNode
from src.nodes.generate_sections_node import GenerateSectionsNode
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.output_format_node import OutputFormatNode
from src.nodes.validation_check_node import ValidationCheckNode, resolve_threshold
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for FIN-C2-063.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by DocumentExtractionGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          → input_validate            (InputValidateNode)
          → extract_document_data     (ExtractDocumentDataNode)
          → generate_sections         (GenerateSectionsNode)
          → validation_check          (ValidationCheckNode)
          → output_format             (OutputFormatNode)
          → END

    All nodes are FunctionNode subclasses with ANONYMOUS trust_level.
    initialize / finalize are outer backbone concerns — not registered here.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "fin_c2_063_financial_document_extraction_workflow"

    @property
    def state_schema(self) -> type:
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """No mandatory config for v1 rule-based inner graph."""
        pass

    # ── Runtime config seeding ────────────────────────────────────────────────

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the forwarded runtime config into the inner initial state.

        The SDK does not thread the graph's ``self.config`` into
        node.execute()'s ``config`` argument on the real ``.invoke()`` path
        (that argument is only populated by direct unit calls). The outer
        GraphNode forwards config/config.yaml's declared settings via
        _parent_config() → ``self.config["configurable"]``; here we copy the
        declared ``min_confidence_threshold`` into the inner state so
        ValidationCheckNode observes it end-to-end. Absent, non-finite or
        out-of-range → omitted, and the node falls back to its declared default.
        """
        configurable = (self.config or {}).get("configurable", {}) or {}
        threshold = resolve_threshold(configurable.get("min_confidence_threshold"))
        if threshold is None:
            return {}
        return {"min_confidence_threshold": threshold}

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all 5 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.
        Every key registered here is referenced in add_edges().
        All nodes are instantiated with empty-parens (no ctor args) —
        No-arg constructor rule: FunctionNode subclasses take no arguments.
        """
        self._nodes["input_validate"] = InputValidateNode()
        self._nodes["extract_document_data"] = ExtractDocumentDataNode()
        self._nodes["generate_sections"] = GenerateSectionsNode()
        self._nodes["validation_check"] = ValidationCheckNode()
        self._nodes["output_format"] = OutputFormatNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear document-extraction topology.

        Linear flow:
            input_validate → extract_document_data → generate_sections
            → validation_check → output_format → END.

        No conditional branching — all paths through the extraction pipeline are
        linear in v1.  route() satisfies the ABC but is not used at runtime.
        """
        self._sg.add_edge(START, "input_validate")
        self._sg.add_edge("input_validate", "extract_document_data")
        self._sg.add_edge("extract_document_data", "generate_sections")
        self._sg.add_edge("generate_sections", "validation_check")
        self._sg.add_edge("validation_check", "output_format")
        self._sg.add_edge("output_format", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: AgentState) -> str:
        """Conditional routing — required by BaseGraph ABC.

        Linear topology; add_conditional_edges() is not used, so this method
        is never called at runtime.  Returns END on error so an unexpected
        invocation does not re-enter a processing node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "output_format"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by DocumentExtractionGraphNode.merge_output()
        in graph.py as the `sub_result` argument.  Both methods are designed
        together to guarantee field-name consistency:

            Inner get_output() emits:   "extraction_report", "extracted_fields",
                                        "validation_flags", "field_confidence",
                                        "min_confidence_met", "status"
            Outer merge_output() reads: sub_result.get(...) for each key above.
        """
        return {
            "extraction_report": state.get("extraction_report"),
            "extracted_fields": state.get("extracted_fields"),
            "validation_flags": state.get("validation_flags"),
            "field_confidence": state.get("field_confidence"),
            "min_confidence_met": state.get("min_confidence_met", False),
            "status": state.get("status"),
            "node_history": state.get("node_history", []),
            "correlation_id": state.get("correlation_id"),
        }
