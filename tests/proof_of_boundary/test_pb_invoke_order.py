# PB-6: Invoke Execution Order Verification
# Verifies BaseNode.__call__() enforces: trust gate -> node_start audit event ->
# input gate -> execute() -> output gate -> node_complete audit event, for every
# concrete node under src/nodes/.
#
# Also verifies the full backbone invoke order for the outer
# FinancialDocumentOCRExtractionAgent (two-layer nested graph):
#   InitializeNode -> PreProcessNode (pre_process) -> DocumentExtractionGraphNode (main)
#   -> PostProcessNode (post_process) -> FinalizeNode
#
# PB-6 invoke uses VERIFIED_EXTERNAL caller trust (the real external path) — NEVER
# for_internal(). A VERIFIED_EXTERNAL InvocationContext exercises the same code path a
# real deployed caller uses: it clears the outer PreProcessNode trust gate
# (required_trust_level = VERIFIED_EXTERNAL) AND passes through the inner ANONYMOUS
# domain nodes. for_internal() (INTERNAL) would not represent a real external caller,
# so it is deliberately not used.

import importlib
import inspect
import json
import pkgutil
from pathlib import Path

import pytest

# ── Template-specific constants ───────────────────────────────────────────────

# Class name of the node in the `main` backbone slot.
_MAIN_SLOT_NODE = "DocumentExtractionGraphNode"

# A SUCCESS-yielding financial-document extraction payload for the backbone invoke
# test. All PreProcessNode required fields present (document_reference,
# document_type, field_schema); mime_type whitelisted; document_type whitelisted;
# and the OCR text carries a "Label: value" line for every schema field so each
# field extracts + validates at high confidence (min_confidence_met = True).
#
# The account number is deliberately an 8-digit form. The platform input gate
# masks personal-data shapes in the request before any template code runs, and a
# 12-digit run is one of those shapes — so a 12-digit account number arrives as a
# redaction placeholder and the report (correctly) reports it as redacted rather
# than extracted. This payload is the demonstration of a REAL extraction, so it
# uses a form that survives to the pipeline; the redaction path has its own
# end-to-end coverage in tests/integration/.
#
# CONTRACT: deploy/invoke_payload.json["input"] MUST equal this exact string —
# the deployment evidence invoke and the PB-6 test must exercise the identical
# payload. test_invoke_payload_matches_pb6 below asserts that equality so the two
# can never drift.
_VALID_PAYLOAD = json.dumps(
    {
        "document_reference": "DOC-2026-000123",
        "document_type": "bank_statement",
        "mime_type": "application/pdf",
        "ocr_engine": "native",
        "field_schema": {
            "account_number": "string",
            "statement_date": "date",
            "closing_balance": "amount",
        },
        "ocr_text": (
            "MONTHLY BANK STATEMENT\n"
            "Account Number: 40021359\n"
            "Statement Date: 2026-06-30\n"
            "Closing Balance: 1,250,000.00\n"
        ),
    }
)

# ─────────────────────────────────────────────────────────────────────────────


def _discover_node_classes() -> list[type]:
    """Import every module under src/nodes/ and collect concrete BaseNode subclasses."""
    from framework.nodes.base_node import BaseNode

    try:
        pkg = importlib.import_module("src.nodes")
    except ImportError:
        return []

    discovered = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
            ):
                discovered.append(attr)
    return discovered


def _patch_domain_emit(monkeypatch):
    """Patch emit_trace_event in every domain node module (avoids audit-backend calls)."""
    for mod_suffix in (
        "pre_process_node",
        "input_validate_node",
        "extract_document_data_node",
        "generate_sections_node",
        "validation_check_node",
        "output_format_node",
        "post_process_node",
    ):
        try:
            monkeypatch.setattr(
                f"src.nodes.{mod_suffix}.emit_trace_event",
                lambda *a, **k: None,
            )
        except AttributeError:
            pass  # module not yet imported / no emit symbol; fine


class TestInvokeOrder:
    """PB-6: __call__ runs trust gate -> node_start -> input gate -> execute()
    -> output gate -> node_complete, in that order, for every node."""

    def test_call_order_for_every_node(self, monkeypatch):
        node_classes = _discover_node_classes()
        if not node_classes:
            pytest.skip("no concrete BaseNode subclasses found under src/nodes/")

        import framework.nodes.base_node as base_node_module

        failures: list[str] = []
        for node_cls in node_classes:
            order: list[str] = []
            monkeypatch.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event_type, _payload, _state, _o=order: _o.append(f"event:{event_type}"),
            )

            for method_name, label in (
                ("_security_gate_input", "security_gate_input"),
                ("execute", "execute"),
                ("_security_gate_output", "security_gate_output"),
            ):
                original = getattr(node_cls, method_name)

                def spy(self, arg, _o=order, _label=label, _orig=original):
                    _o.append(_label)
                    return _orig(self, arg)

                monkeypatch.setattr(node_cls, method_name, spy)

            instance = node_cls()
            # caller trust == the node's required level so the trust gate always passes
            # here; the gate-denial branch is asserted separately in TestTrustGate.
            state = {
                "caller_trust_level": node_cls.required_trust_level.value,
                "correlation_id": "pb6-invoke-order-test",
            }
            instance(state)

            expected = [
                "event:node_start",
                "security_gate_input",
                "execute",
                "security_gate_output",
                "event:node_complete",
            ]
            if order != expected:
                failures.append(
                    f"{node_cls.__name__}: invoke order violation.\n" f"expected: {expected}\nactual:   {order}"
                )

        assert not failures, "\n\n".join(failures)


class TestTrustGate:
    """PB-6: the trust gate in BaseNode.__call__ runs BEFORE execute() and denies
    a caller whose trust is below the node's required_trust_level."""

    def test_pre_process_denies_anonymous_caller(self, monkeypatch):
        """PreProcessNode (required VERIFIED_EXTERNAL) must refuse an ANONYMOUS caller."""
        _patch_domain_emit(monkeypatch)
        from framework.schemas.agent_status import AgentStatus
        from framework.schemas.trust_level import TrustLevel
        from src.nodes.pre_process_node import PreProcessNode

        node = PreProcessNode()
        result = node(
            {
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
                "user_input": _VALID_PAYLOAD,
                "correlation_id": "pb6-trust-denial",
            }
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert any(
            "trust gate" in e.lower() for e in result.get("error_log", [])
        ), f"expected a trust-gate denial, got error_log={result.get('error_log')}"

    def test_pre_process_admits_verified_external_caller(self, monkeypatch):
        """The same node admits a VERIFIED_EXTERNAL caller and runs execute() to SUCCESS."""
        _patch_domain_emit(monkeypatch)
        from framework.schemas.agent_status import AgentStatus
        from framework.schemas.trust_level import TrustLevel
        from src.nodes.pre_process_node import PreProcessNode

        node = PreProcessNode()
        result = node(
            {
                "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
                "user_input": _VALID_PAYLOAD,
                "input_context": {},
                "correlation_id": "pb6-trust-admit",
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] is not None


class TestBackboneInvokeOrder:
    """PB-6 backbone: a full Graph().invoke() runs the 5-node backbone in order.

    Backbone order: InitializeNode -> PreProcessNode (pre_process) ->
                    DocumentExtractionGraphNode (main) ->
                    PostProcessNode (post_process) -> FinalizeNode

    Uses VERIFIED_EXTERNAL caller trust — the real external path.
    InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL) is mandatory;
    NEVER use for_internal(), which would not represent a real external caller.
    """

    def _invoke(self, monkeypatch):
        _patch_domain_emit(monkeypatch)
        from framework.schemas.invocation_context import InvocationContext, TrustLevel
        from src.graph.graph import Graph

        agent = Graph()
        agent.compile()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        return agent.invoke(_VALID_PAYLOAD, ctx=ctx)

    def test_backbone_invoke_succeeds_and_returns_output(self, monkeypatch):
        from framework.schemas.agent_status import AgentStatus

        result = self._invoke(monkeypatch)
        assert result.get("status") == AgentStatus.SUCCESS.value, (
            f"Expected status={AgentStatus.SUCCESS.value!r}, got: {result.get('status')!r}\n"
            f"error_log: {result.get('error_log')}"
        )
        assert result.get("output") is not None, "output must be set after a successful invoke"
        # The assembled schema-scoped extraction report must be present in the output.
        assert "DOC-2026-000123" in result["output"]
        assert "extraction_status" in result["output"]
        assert "account_number" in result["output"]
        # The report must carry the values READ FROM THE DOCUMENT — not just the
        # requested field names — so a pipeline that stopped depending on its
        # input would fail here.
        report = json.loads(result["output"])
        assert report["fields"]["account_number"]["value"] == "40021359"
        assert report["fields"]["statement_date"]["value"] == "2026-06-30"
        assert report["fields"]["closing_balance"]["value"] == "1,250,000.00"
        assert report["extraction_status"] == "complete"

    def test_backbone_node_history_matches_expected_order(self, monkeypatch):
        result = self._invoke(monkeypatch)
        history = result.get("node_history", [])
        assert history == [
            "InitializeNode",
            "PreProcessNode",
            "DocumentExtractionGraphNode",
            "PostProcessNode",
            "FinalizeNode",
        ], f"unexpected backbone node_history: {history}"

    def test_main_slot_is_document_extraction_graph_node(self):
        """The `main` backbone slot must be DocumentExtractionGraphNode (a GraphNode — Cat 2)."""
        from framework.nodes.graph_node import GraphNode
        from src.graph.graph import (
            DocumentExtractionGraphNode,
            FinancialDocumentOCRExtractionAgent,
        )

        agent = FinancialDocumentOCRExtractionAgent()
        agent.compile()
        main_node = agent._nodes.get("main")
        assert main_node is not None, "main slot must be registered"
        assert isinstance(
            main_node, DocumentExtractionGraphNode
        ), f"main slot must be DocumentExtractionGraphNode, got {type(main_node).__name__}"
        assert isinstance(main_node, GraphNode), "main slot node must subclass GraphNode (Cat 2 contract)"
        assert main_node.__class__.__name__ == _MAIN_SLOT_NODE

    def test_invoke_payload_matches_pb6(self):
        """deploy/invoke_payload.json["input"] MUST equal _VALID_PAYLOAD.

        The deployment evidence invoke posts invoke_payload.json as the request
        body, so it must exercise the same payload PB-6 asserts yields SUCCESS.
        """
        repo_root = Path(__file__).resolve().parents[2]
        payload_file = repo_root / "deploy" / "invoke_payload.json"
        assert payload_file.exists(), "deploy/invoke_payload.json is required for deployment"
        body = json.loads(payload_file.read_text())
        assert (
            body.get("input") == _VALID_PAYLOAD
        ), "deploy/invoke_payload.json['input'] must equal the PB-6 _VALID_PAYLOAD"
        # And the payload the server forwards to agent.invoke() must itself be a
        # valid, PreProcessNode-parseable extraction-request JSON object.
        request = json.loads(body["input"])
        for required in ("document_reference", "document_type", "field_schema"):
            assert required in request, f"invoke_payload input missing required field: {required}"
