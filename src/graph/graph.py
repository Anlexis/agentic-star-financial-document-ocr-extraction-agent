"""AgentCore Platform v1.0"""

# FIN-C2-063 — Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed — do NOT override add_edges()):
#     START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                             ↓ (RETRY, max 3)
#                                          pre_process
#
#   `main` slot is a GraphNode subclass (DocumentExtractionGraphNode) that
#   delegates the full domain workflow to DomainWorkflowGraph (inner BaseGraph).
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 ← outer graph (this file)
#   src/graph/domain_workflow_graph.py ← inner graph (multi-step topology)
#
# Rules enforced:
#   ✅ FinancialDocumentOCRExtractionAgent inherits AgentBaseGraph (L1 Base)
#   ✅ super().register_nodes() called first (fills initialize + finalize)
#   ✅ DocumentExtractionGraphNode assigned to self._nodes["main"]
#   ✅ PreProcessNode (VERIFIED_EXTERNAL) in pre_process slot (trust gate)
#   ✅ PostProcessNode (ANONYMOUS) in post_process slot (output gate)
#   ✅ merge_output() returns only changed keys
#   ✅ get_output() surfaces the domain result on the gated success path only
#   ✅ class name matches config/agent.yaml class: field exactly
#   ❌ add_edges() NOT overridden on the outer graph
#   ❌ No platform SDK imports

import os
from typing import TYPE_CHECKING, Any, ClassVar, Dict

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.trust_level import TrustLevel
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.validation_check_node import resolve_threshold
from src.schemas.state import State

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.graph.domain_workflow_graph import DomainWorkflowGraph

# Runtime parameters — config/config.yaml at the repo root (three levels up from
# this file: src/graph/graph.py → src/graph → src → <repo root>). This is the
# ONLY runtime-config source: config/agent.yaml is the static registry manifest
# and carries no runtime block.
_RUNTIME_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config",
    "config.yaml",
)


def load_runtime_config() -> Dict[str, Any]:
    """Return the runtime parameters declared in config/config.yaml.

    Best-effort: a missing or unparseable file yields ``{}`` so graph
    construction never breaks (nodes then fall back to their declared
    defaults). PyYAML is loaded lazily — it is a framework runtime dependency,
    so importing it on demand avoids a hard module-load coupling.
    """
    try:
        import yaml

        with open(_RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


class DocumentExtractionGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of the agent.

    Wraps DomainWorkflowGraph (the inner BaseGraph).
    Called by the AgentBaseGraph backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()  — instantiate and return DomainWorkflowGraph
      extract_input() — pull validated_input from outer state
      merge_output()  — map sub_result fields into outer state delta (changed keys only)
      error_strategy  — "propagate": re-raise inner errors as SubgraphError (fail-fast)
    """

    # S-1 declared on the wrapper too: the CI gate only AST-scans FunctionNode
    # subclasses, so a GraphNode main slot passes the pipeline without one and is
    # flagged at review. Same level the nodes in this repo already declare.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> "DomainWorkflowGraph":
        """Instantiate and return the inner domain workflow graph.

        DomainWorkflowGraph is imported lazily (inside the method) to avoid
        circular-import risk at module load time and to match the nested pattern.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        """Return the string input passed into inner_graph.invoke().

        PreProcessNode validates and normalises the raw user_input and writes
        the result to validated_input.  Prefer that; fall back to user_input if
        validated_input is absent (e.g. in unit tests).
        """
        value = state.get("validated_input", state.get("user_input", ""))
        return value if isinstance(value, str) else ""

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map inner graph sub_result back into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output().
        Returns ONLY changed keys — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output() emits  → "extraction_report", "extracted_fields",
                                       "validation_flags", "field_confidence",
                                       "min_confidence_met", "status"
          This merge_output() reads → sub_result.get(...) for each of these keys.

        PostProcessNode (outer post_process) reads extraction_report from state
        to apply the output gate and set formatted_output.
        """
        return {
            "extraction_report": sub_result.get("extraction_report"),
            "extracted_fields": sub_result.get("extracted_fields"),
            "validation_flags": sub_result.get("validation_flags"),
            "field_confidence": sub_result.get("field_confidence"),
            "min_confidence_met": sub_result.get("min_confidence_met", False),
            "status": sub_result.get("status"),
        }

    def _parent_config(self) -> Dict[str, Any]:
        """Forward the declared runtime config to the inner graph.

        Reads config/config.yaml — the live runtime-parameter file — and exposes
        the declared settings under the LangGraph ``configurable`` key.
        DomainWorkflowGraph._extra_initial_state() reads this to seed the inner
        state so ValidationCheckNode actually observes the configured threshold
        on the real ``.invoke()`` path (the SDK does not thread runtime config
        into node.execute()). Only declared keys are forwarded; absent keys fall
        back to node defaults.

        ``min_confidence_threshold`` passes through the finite+bounded parser
        here as well as at the consumer, so a malformed declaration degrades to
        the node default instead of reaching a comparison that would silently
        never be satisfied.
        """
        cfg = load_runtime_config()
        llm = cfg.get("llm", {}) or {}
        declared: Dict[str, Any] = {
            "min_confidence_threshold": resolve_threshold(cfg.get("min_confidence_threshold")),
            "system_prompt_template": llm.get("system_prompt_template"),
            "temperature": llm.get("temperature"),
            "max_tokens": llm.get("max_tokens"),
        }
        return {"configurable": {k: v for k, v in declared.items() if v is not None}}


class FinancialDocumentOCRExtractionAgent(AgentBaseGraph):
    """Outer graph for FIN-C2-063 (Cat 2 — document-generation pattern).

    Inherits AgentBaseGraph directly (L1 Base). Domain logic is fully
    encapsulated in DocumentExtractionGraphNode (main slot), which delegates to
    DomainWorkflowGraph (inner BaseGraph).

    Backbone (fixed):
        START → initialize → pre_process → main → post_process → finalize → END

    register_nodes() and get_output() are the only overrides:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process: PreProcessNode    (VERIFIED_EXTERNAL — trust gate)
      - main:        DocumentExtractionGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode  (ANONYMOUS — output gate)
      - get_output(): surfaces the domain result on the gated success path

    add_edges() is NOT overridden — backbone wiring belongs to the framework.

    Class name MUST match config/agent.yaml `class:` field exactly.
    server.py imports this as `Graph` via the alias below.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with AgentRegistry."""
        return "FinancialDocumentOCRExtractionAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = DocumentExtractionGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Surface the domain extraction result on the outer invoke() return.

        AgentBaseGraph.get_output() returns only the minimal
        ``{output, status, trace_id, correlation_id, node_history}`` envelope.
        On the compiled outer-graph success path that dropped the structured
        domain result — the fields the inner DomainWorkflowGraph produces
        (merged into outer state by DocumentExtractionGraphNode.merge_output():
        extraction_report, extracted_fields, validation_flags, field_confidence,
        min_confidence_met) and the gated PostProcessNode outputs
        (formatted_output, result) — from the dict returned by
        ``agent.invoke()``. They were all None to the caller even on a
        successful extraction. This override extends the base envelope so a
        successful invocation actually returns the domain result.

        Output-gate invariant preserved (fail-closed):
          * The base envelope's ``output`` key resolves as
            ``formatted_output or result``. ``result`` in outer state is written
            ONLY by PostProcessNode — the gate — so on the gated success path
            the fallback is safe. On any NON-success outcome the fallback is
            closed off explicitly: ``output`` is re-resolved as
            ``formatted_output or None`` and ``result`` is withheld, so an error
            branch that returns without writing those keys (a node exception
            leaves a partial delta that clears nothing, and LangGraph keeps the
            previous value) cannot surface un-gated text through the envelope.
          * The structured fields (extraction_report / extracted_fields /
            validation_flags / field_confidence / min_confidence_met) are
            surfaced ONLY when status == SUCCESS. Every field present in
            extracted_fields on a SUCCESS invoke was already cleared by the
            InputValidateNode output-authorization gate (an unauthorised
            sensitive field fails the invoke before extraction), so this does
            not weaken the fail-closed authorization.
        """
        output: Dict[str, Any] = super().get_output(state)
        succeeded = state.get("status") == AgentStatus.SUCCESS.value

        if succeeded:
            output["formatted_output"] = state.get("formatted_output")
            output["result"] = state.get("result")
            output["extraction_report"] = state.get("formatted_output") or state.get("result")
            output["extracted_fields"] = state.get("extracted_fields")
            output["validation_flags"] = state.get("validation_flags")
            output["field_confidence"] = state.get("field_confidence")
            output["min_confidence_met"] = state.get("min_confidence_met")
            return output

        # Non-success: the only value that may reach the caller is the gate's own
        # refusal notice. Everything derived from the un-gated inner answer is
        # withheld, and the base envelope's `or result` fallback is closed.
        notice = state.get("formatted_output") or None
        output["output"] = notice
        output["formatted_output"] = notice
        output["result"] = None
        output["extraction_report"] = None
        output["extracted_fields"] = None
        output["validation_flags"] = None
        output["field_confidence"] = None
        output["min_confidence_met"] = None
        return output

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


# Alias for backward compat (server.py imports Graph).
# Class name FinancialDocumentOCRExtractionAgent matches config/agent.yaml class: field.
Graph = FinancialDocumentOCRExtractionAgent
