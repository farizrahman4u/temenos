"""FastAPI control plane over a BoxManager.

REST so the CLI (and programmatic clients) can drive one shared daemon. Endpoints are
sync `def` — FastAPI runs them in a threadpool, which fits the blocking subprocess core.
Bearer-token auth (the token is minted by `temenos serve` and shared via the daemon-info
file). The per-box MCP sub-app mounts here in Phase 5.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from ..exceptions import BoxNotFound, TemenosError
from ..manager import BoxManager
from ..policy import Policy
from .mcp import BoxMCPRouter


class CreateBox(BaseModel):
    data_dir: str
    policy: dict = {}
    name: str | None = None
    restore_from: str | None = None


class ExecBody(BaseModel):
    cmd: list[str]
    cwd: str | None = None
    timeout: float | None = None
    stdin: str | None = None


def create_app(manager: BoxManager | None = None, token: str | None = None) -> FastAPI:
    manager = manager or BoxManager()
    token = token if token is not None else os.environ.get("TEMENOS_DAEMON_TOKEN")

    # Per-box MCP data plane mounted at /mcp/<id>. Its streamable session manager needs a
    # running task group, supplied by the app lifespan below.
    mcp_router = BoxMCPRouter(manager, token)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp_router.session_manager.run():
            yield

    app = FastAPI(title="temenos", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.manager = manager
    app.mount("/mcp", mcp_router)

    def auth(authorization: str = Header(default="")) -> None:
        if token and authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="invalid or missing token")

    @app.exception_handler(BoxNotFound)
    def _nf(_req, exc):  # type: ignore[no-untyped-def]
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"detail": f"box not found: {exc}"})

    @app.exception_handler(TemenosError)
    def _te(_req, exc):  # type: ignore[no-untyped-def]
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/v1/boxes", dependencies=[Depends(auth)])
    def create_box(body: CreateBox) -> dict:
        bid = manager.create(body.data_dir, Policy.from_dict(body.policy),
                             name=body.name, restore_from=body.restore_from)
        return manager.info(bid)

    @app.get("/v1/boxes", dependencies=[Depends(auth)])
    def list_boxes() -> list[dict]:
        return manager.list()

    @app.get("/v1/boxes/{bid}", dependencies=[Depends(auth)])
    def get_box(bid: str) -> dict:
        return manager.info(bid)

    @app.delete("/v1/boxes/{bid}", dependencies=[Depends(auth)])
    def delete_box(bid: str) -> dict:
        manager.delete(bid)
        return {"deleted": bid}

    @app.post("/v1/boxes/{bid}/exec", dependencies=[Depends(auth)])
    def exec_box(bid: str, body: ExecBody) -> dict:
        return manager.get(bid).exec(body.cmd, cwd=body.cwd, timeout=body.timeout,
                                     stdin=body.stdin).to_dict()

    @app.get("/v1/boxes/{bid}/attach", dependencies=[Depends(auth)])
    def attach_box(bid: str) -> dict:
        # Pieces for a LOCAL interactive `runsc exec` (PTY passthrough). Only meaningful to
        # a same-host, same-user client (the CLI) — a PTY can't stream over this REST path.
        return manager.get(bid).attach_context()

    @app.get("/v1/boxes/{bid}/audit", dependencies=[Depends(auth)])
    def audit_box(bid: str) -> list[dict]:
        return manager.get(bid).audit.to_dicts()

    @app.get("/v1/boxes/{bid}/writes", dependencies=[Depends(auth)])
    def writes_box(bid: str) -> dict:
        return {"writes": manager.get(bid).writes()}

    return app
