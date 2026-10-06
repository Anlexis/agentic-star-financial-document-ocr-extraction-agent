# Financial Document OCR Extraction Agent

AI agent for extracting structured data from OCR text of financial documents, built with Agentic Star.

> **Category**: Cat 2 (domain-specific multi-step pipeline)
> **Industry**: Finance
> **Template ID**: FIN-C2-063

## Overview

Extracts a caller-defined set of structured fields from a financial document — a loan
application, KYC record, bank statement, trade confirmation or invoice — and returns them as
JSON with a per-field validity flag and confidence score.

The caller submits a document reference, the document type, and the field schema it wants
back (`{"account_number": "string", "statement_date": "date", "closing_balance": "amount"}`),
optionally together with the document's already-extracted text. The agent validates the
request, parses the text into labelled blocks, extracts only the fields the schema declares,
checks each value against its declared type, and reports whether the whole extraction cleared
a configurable confidence threshold. Requested fields that are sensitive for the given
document type require explicit caller authorization, and anything the platform's input filter
masked as personal data is reported as redacted rather than as an extracted value.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode: if the platform is unreachable or the framework version does not match, graph compile
and start-up preflight raise rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

### Caller authentication

The HTTP entry point requires an authenticated caller. Set `INVOKE_AUTH_TOKEN` in the server
environment and present it as `Authorization: Bearer <token>`; the request then runs at
`VERIFIED_EXTERNAL`, which is the trust level this agent's input node requires. With no token
configured, every caller stays anonymous and every request is refused before any extraction
runs.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Request and response

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

```json
{
  "document_reference": "DOC-2026-000123",
  "extraction_status": "complete",
  "min_confidence_met": true,
  "field_count": 3,
  "fields": {
    "account_number": {"value": "40021359", "valid": true, "reason": "ok", "confidence": 0.95}
  }
}
```

## Project Structure

```
src/          agent implementation (nodes, graphs, policies, schemas)
tests/        unit, integration and boundary tests
config/       agent manifest (agent.yaml) and runtime parameters (config.yaml)
docs/         design and test documentation
```

See `docs/02_design.md` for the architecture and `docs/03_test_spec.md` for the test contract.

## Customising

1. Adjust `config/config.yaml` for your own confidence threshold and runtime limits.
2. Adjust the approved document types and their field allowlists in `src/policy/pii_policy.py`.
3. Wire a real document-text source in `src/nodes/extract_document_data_node.py` — this
   version reads caller-supplied text only.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
