"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, the gateway calls agent.invoke() directly.

import os
import secrets
from typing import Any, Dict
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import Graph, load_runtime_config

app = FastAPI(title="Agent")

# Runtime parameters come from config/config.yaml. Constructing the graph bare
# would leave self.config empty, so every declared value (max_retry, timeout_s,
# min_confidence_threshold) would be inert on this entry point while still
# appearing in the shipped configuration.
agent = Graph(config=load_runtime_config())
agent.compile()
# namespace/agent_name match the manifest values in config/agent.yaml.
agent.provision_secrets(secrets_factory(namespace="fin-c2-063", agent_name="FinancialDocumentOCRExtractionAgent"))


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Dict[str, Any]:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller auth: when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and run at VERIFIED_EXTERNAL.
    # Middleware-established trust is never demoted. This adapter is the
    # entry-point auth boundary (the standalone equivalent of the platform's
    # auth middleware) — a deployment-level caller credential, not an agent
    # secret, so ctx.secrets does not apply: no InvocationContext exists before
    # auth runs.
    #
    # INVOKE_AUTH_TOKEN is REQUIRED for this agent, not optional. The pre_process
    # node requires VERIFIED_EXTERNAL, so with no token configured every caller
    # stays ANONYMOUS and every request is refused by the trust gate before any
    # extraction runs.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        envelope: Dict[str, Any] = agent.invoke(req.input, ctx=ctx)
        return envelope


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "FinancialDocumentOCRExtractionAgent"}
