# Test Specification — FIN-C2-063 Financial Document OCR & Extraction Agent

## 1. Test strategy

- **Agent:** FIN-C2-063 — Financial Document OCR & Extraction Agent (Cat 2,
  document-generation pattern, two-layer nested graph: outer `AgentBaseGraph` backbone +
  inner `DomainWorkflowGraph` `BaseGraph`).
- **Coverage target:** ≥ 90% of `src/nodes/`, `src/graph/` and `src/policy/` branches.
- **Test types:** Unit (per node, per policy module, graph wiring) · Proof-of-Boundary
  (framework security/serialisation contracts) · Integration (full compiled
  `Graph().invoke()` as a real VERIFIED_EXTERNAL caller).
- **Framework provisioning:** `framework` (agenticstar-agentcore) is supplied by CI. Tests
  import the real modules; there are no stub nodes.
- **Audit events:** `emit_trace_event` is patched at the node module level in tests to avoid
  audit-backend calls, never via a `sys.modules` stub (which would break the real `shared`
  package the framework loads at import time).
- **Raw document bytes** never enter State — only the `document_reference` identifier is
  carried; document text is derived and schema-scoped before the output gate.

### Test file map

| File | Scope |
|------|-------|
| `tests/unit/test_nodes.py` | All 6 domain/backbone nodes + outer & inner graph wiring + config propagation |
| `tests/unit/test_content_policy.py` | Control-token screen + inert-identifier locks + redaction-sentinel recognition, both directions |
| `tests/unit/test_pii_policy.py` | Sensitivity classification, field authorization, output PII redaction |
| `tests/unit/test_framework_compliance_tc06_tc07.py` | Framework gate methods are non-bypassable |
| `tests/integration/test_outer_invoke_returns_domain_result.py` | Full outer invoke: domain result surfaced, error-envelope containment, redaction honesty, control tokens refused end-to-end |
| `tests/proof_of_boundary/test_pb_invoke_order.py` | PB-6 per-node + backbone invoke order (VERIFIED_EXTERNAL) + trust gate + payload alignment |
| `tests/proof_of_boundary/test_import_isolation.py` | PB-4 platform-SDK import isolation (AST scan) |
| `tests/proof_of_boundary/test_state_safety.py` | PB-2/PB-5 State msgpack/credential safety (AST scan) |
| `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` | PB-7 interrupt propagation (skip stub — no cross-boundary interrupt) |

### Canonical valid payload (PB-6 `_VALID_PAYLOAD`)

The extraction request used by the backbone invoke test and by `deploy/invoke_payload.json`
(the two MUST stay identical — asserted by `test_invoke_payload_matches_pb6`). Every schema
field has a matching `Label: value` line, so all fields extract and validate at confidence
0.95 ≥ the 0.80 threshold ⇒ `min_confidence_met = True`, `extraction_status = "complete"`:

```json
{
  "document_reference": "DOC-2026-000123",
  "document_type": "bank_statement",
  "mime_type": "application/pdf",
  "ocr_engine": "native",
  "field_schema": {
    "account_number": "string",
    "statement_date": "date",
    "closing_balance": "amount"
  },
  "ocr_text": "MONTHLY BANK STATEMENT\nAccount Number: 40021359\nStatement Date: 2026-06-30\nClosing Balance: 1,250,000.00\n"
}
```

The account number is an 8-digit form on purpose. The platform input filter masks
personal-data shapes in the request before any template code runs, and a 12-digit run is one
of them, so a 12-digit account number arrives as a redaction placeholder and is reported as
redacted rather than extracted. This payload is the demonstration of a *real* extraction; the
redaction path has its own end-to-end coverage
(`TestRedactedValuesAreReportedHonestly`).

## 2. Framework compliance tests (mandatory)

| TC-ID | Test | Expected Result | Where |
|-------|------|----------------|-------|
| TC-01 | State contract: flat `TypedDict`, domain fields `NotRequired`, no Pydantic/dataclass | AST scan: 0 violations | `test_state_safety.py` |
| TC-02 | Invalid/empty/non-JSON input rejected at PreProcessNode | `status=error`, error_log populated | `TestPreProcessNode` |
| TC-03 | No credential in State | CI credential gate: 0 violations | CI + `test_state_safety.py` |
| TC-04 | `execute(self, state)` contract | Signature `(self, state)` | `test_execute_signature_is_state_first` |
| TC-05 | Audit: `emit_trace_event()` called inside each node `execute()` | ≥1 domain event per node | `scripts/check_audit_trace.py` (CI gate) |
| TC-06/07 | Framework input/output gate methods cannot be overridden | TypeError at class definition | `test_framework_compliance_tc06_tc07.py` |
| TC-08 | `required_trust_level` enforced in `__call__` before `execute()` | ANONYMOUS caller → refused; VERIFIED_EXTERNAL → admitted | `TestTrustGate` |
| TC-08a | Outer `PreProcessNode` = VERIFIED_EXTERNAL; inner nodes + post_process = ANONYMOUS | trust levels asserted per node | `test_trust_level_*` |
| TC-11 | Output gate on post_process | credential pattern → withheld + `status=error`; clean → pass | `TestPostProcessNode` |

## 3. Proof-of-Boundary tests (mandatory)

| PB-ID | Boundary | Test | Expected Result | Where |
|-------|----------|------|----------------|-------|
| PB-2 | State serialization | AST scan of `src/schemas/state.py` | primitives only; no Pydantic/dataclass | `test_state_safety.py` |
| PB-4 | Import isolation | AST scan of `src/` | 0 platform-SDK imports | `test_import_isolation.py` |
| PB-5 | Checkpoint safety | no credential-named fields / prohibited types in State | inspection pass | `test_state_safety.py` |
| PB-6 | Invoke execution order (per node) | `__call__`: node_start → trust gate → input gate → `execute()` → output gate → node_complete | order verified for every `src/nodes/` class | `TestInvokeOrder` |
| PB-6b | Backbone invoke order | full `Graph().invoke(_VALID_PAYLOAD, ctx=VERIFIED_EXTERNAL)` | `status=success`; node_history = `[Initialize, PreProcess, DocumentExtractionGraphNode, PostProcess, Finalize]`; the report carries the values read from the document | `TestBackboneInvokeOrder` |
| PB-6c | Real external caller | `InvocationContext(caller_trust_level=VERIFIED_EXTERNAL)` — **never** `for_internal()` | inner ANONYMOUS nodes accept the passthrough trust; SUCCESS end-to-end | `TestBackboneInvokeOrder` |
| PB-6d | Trust-gate denial (node level) | `PreProcessNode` called with ANONYMOUS caller | `status=error`; "trust gate" in error_log; VERIFIED_EXTERNAL admitted | `TestTrustGate` |
| PB-6e | Payload alignment | `deploy/invoke_payload.json["input"] == _VALID_PAYLOAD` | the deployment evidence invoke exercises the PB-6 payload | `test_invoke_payload_matches_pb6` |
| PB-7 | Interrupt propagation | skip stub — `propagate_hitl=False`, no cross-boundary interrupt checkpoint | skipped with reason | `test_pb7_hitl_interrupt_propagation.py` |

## 4. Business logic tests

| BL-ID | Test | Input | Expected Result | Where |
|-------|------|-------|-----------------|-------|
| BL-01 | Happy-path extraction | `_VALID_PAYLOAD` | schema-scoped JSON report with the values read from the document; SUCCESS | `test_backbone_invoke_succeeds_and_returns_output`, `TestInnerDomainGraph` |
| BL-02 | Request validation (domain) | `document_type` allowlist, `field_schema` well-formedness, `document_reference` non-empty | invalid → `status=error` with the offending field named | `TestInputValidateNode` |
| BL-03 | Document-text block parsing | text with `Label: value` lines | one block per non-blank line; engine fallback to `native`; empty text ≠ hard error | `TestExtractDocumentDataNode` |
| BL-04 | Schema-scoped field extraction | blocks + `field_schema` | only schema-declared keys emitted; matched → value, unmatched → `null` | `TestGenerateSectionsNode` |
| BL-04a | Output depends on input | two different documents, same schema | different extracted values | `test_output_depends_on_the_document_text` |
| BL-05 | Confidence scoring + 0.80 threshold | present+valid → 0.95, present+bad-format → 0.55, missing → 0.0 | `min_confidence_met = (min score ≥ threshold)` | `TestValidationCheckNode` |
| BL-06 | Format validators | `amount` / `date` / `string` values | amount/date regex enforced; string = any non-empty | `test_format_invalid_amount_is_low_confidence`, `test_bad_date_format_is_flagged` |
| BL-07 | Confidence-threshold config propagation | `min_confidence_threshold` declared in `config/config.yaml`, forwarded outer→inner | direct/unit: `config` param honoured; real invoke: the declared value is applied end-to-end; server constructs the graph with the declared config | `TestConfigPropagation` |
| BL-07a | Non-finite / out-of-range threshold | `"NaN"`, `"Infinity"`, `float("nan")`, `1.5`, `-0.1`, `True`, non-numeric | falls back to the declared default; never an unusable comparison | `test_non_finite_or_out_of_range_threshold_falls_back_to_the_default` |
| BL-08 | Report assembly | extracted_fields + flags + confidence | `extraction_status` complete/low_confidence; per-field value/valid/reason/confidence | `TestOutputFormatNode` |
| BL-09 | Graph key coupling | inner `get_output` ↔ outer `merge_output` | 6 coupled keys mapped; `merge_output` returns changed keys only | `TestOuterGraphComposition`, `TestInnerDomainGraph` |
| BL-10 | Output-authorization (pre-extraction) | sensitive field w/o `sensitive_fields_authorized` → reject; with flag → allow; off-allowlist field → reject | fail-closed, audited, before any extraction | `TestInputValidateNode`, `TestAuthorizeFields` |
| BL-11 | Output PII redaction | report containing SSN / card / e-mail values | PII values masked, financial values preserved, `status=success` | `test_pii_values_in_the_report_are_redacted`, `test_legitimate_financial_values_not_redacted`, `TestRedactPII` |
| BL-12 | Redacted values reported honestly | a field the platform masked | `valid: false`, `reason: value_redacted_before_extraction`, `confidence: 0.0`, `extraction_status: low_confidence` | `TestValidationCheckNode`, `TestRedactedValuesAreReportedHonestly` |

## 5. Caller-content and containment tests

| ID | Property | Expected Result | Where |
|----|----------|-----------------|-------|
| CC-01 | Chat-template control tokens refused as a class | `<|im_start|>`, `[INST]`, `<<SYS>>` in the document text, in a field NAME, and split by markup → `status=error` | `TestPreProcessCallerContentScreen`, `TestCallerContentRefusedEndToEnd` |
| CC-02 | The screen is the template's own | asserted by calling `execute()` directly, with no framework wrapper in front | `TestPreProcessCallerContentScreen` |
| CC-03 | `<<SYS>>` is not covered upstream | the platform detector returns no findings for it; the template screen does | `test_the_system_marker_class_is_the_templates_own_responsibility` |
| CC-04 | Screens do not fire on real document text | "Please ignore any previous statement…", "System reference: core banking [batch 4]", `Value < 100 and > 10` → accepted | `test_realistic_document_text_is_not_flagged`, `test_ordinary_financial_text_is_not_refused` |
| CC-05 | Rejections name the location, never the value | the offending substring never appears in error_log or the envelope | `test_refusal_names_the_location_but_never_the_value` |
| CC-06 | Caller strings that render into the report are inert | free-text / overlong `document_reference`, non-identifier field name or type → refused; real references accepted | `TestPreProcessCallerContentScreen`, `TestInertIdentifiers` |
| CC-07 | Structural bounds | > 64 schema entries refused; exactly 64 accepted; > 1,000,000-char text refused without echoing it | `test_schema_entry_cap_is_enforced`, `test_schema_at_the_cap_is_accepted`, `test_oversized_ocr_text_is_refused_without_echoing_it` |
| EC-01 | Error envelope withholds every derived field | a gated violation → `output` = the refusal notice; `result`, `extraction_report`, `extracted_fields`, `validation_flags`, `field_confidence`, `min_confidence_met` all None | `TestErrorEnvelopeContainment` |
| EC-02 | The refusal notice is truthy | a falsy notice would re-open the envelope's `formatted_output or result` fallback | `test_credential_block_withholds_every_derived_field` |
| EC-03 | Clearing is by PRESENCE, not omission | every output-bearing key is present in the returned delta with its cleared value | `test_violation_clears_every_output_bearing_field_by_PRESENCE` |
| EC-04 | No released text, traceback or source path in the error envelope | document content, the matched value, "Traceback" and `/src/nodes/` all absent | `test_error_envelope_carries_no_released_text_or_diagnostics` |
| EC-05 | The block happened AT the gate | `PostProcessNode` present in `node_history` on the blocked invoke | `TestErrorEnvelopeContainment` |
| EC-06 | Clean-path control | the same request still produces its real answer and the gate node still runs | `test_the_gate_node_actually_runs_on_the_clean_path` |
| EC-07 | The gate is never narrower than the framework detector | every framework credential shape is caught by the template gate; template-only shapes are caught too | `test_gate_delegates_to_the_framework_detector`, `test_gate_adds_shapes_the_framework_does_not_carry` |

### Negative / boundary cases

| Case | Node | Expected |
|------|------|----------|
| empty `user_input` | PreProcessNode | `status=error`, "empty" |
| invalid JSON | PreProcessNode | `status=error`, "invalid JSON" |
| JSON root not an object | PreProcessNode | `status=error`, "object" |
| missing `field_schema` | PreProcessNode | `status=error`, "field_schema" |
| disallowed `mime_type` | PreProcessNode | `status=error`, "mime_type" |
| non-inert `document_reference` | PreProcessNode | `status=error`, field named, value never echoed |
| non-identifier field name / type | PreProcessNode | `status=error`, entry index or field named |
| oversized / non-string `ocr_text` | PreProcessNode | `status=error`, length reported, content never echoed |
| control token anywhere in the request | PreProcessNode | `status=error`, location + token class |
| empty `document_reference` | InputValidateNode | `status=error`, "document_reference" |
| disallowed `document_type` | InputValidateNode | `status=error`, "document_type" |
| empty `field_schema` | InputValidateNode | `status=error`, "field_schema" |
| sensitive field w/o authorization | InputValidateNode | `status=error`, "authoriz" (fail-closed) |
| off-allowlist field | InputValidateNode | `status=error`, offending field named |
| invalid JSON | ExtractDocumentDataNode | `status=error`, "JSON" |
| missing `field_schema` | Generate / Validation | `status=error` |
| missing `extracted_fields` | OutputFormatNode | `status=error` |
| empty `extraction_report` | PostProcessNode | fallback message, `status=success` |
| credential in output | PostProcessNode | withheld, `status=error`, every derived field cleared |
| PII value in output | PostProcessNode | masked, `status=success` |
| redaction placeholder as a value | ValidationCheckNode | `valid=false`, `confidence=0.0`, reason `value_redacted_before_extraction` |

## 6. Execution summary

- `pytest tests/` — **194 passed, 1 skipped** (PB-7 skip stub, by design) against the real
  framework wheel.
- Gates: dependency pinning, stub-check, category consistency, import isolation, invoke
  chain, credential scan, trust level, audit trace, manifest schema, OSS licence,
  forbidden strings, repository structure — all PASS. `ruff check` and
  `ruff format --check` clean; `mypy src` clean.
- Coverage: node, graph and policy modules exercised on success and error paths, including
  output-authorization, PII redaction, redaction honesty, control-token refusal, envelope
  containment, and the confidence-threshold propagation contract (inner + outer invoke).
