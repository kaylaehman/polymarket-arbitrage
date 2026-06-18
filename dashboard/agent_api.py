"""
Agent Control API
=================

HTTP surface so an EXTERNAL agent (the user's OpenClaw agent) can read state and
control the bot. Mounted on the dashboard's FastAPI app.

Security model:
- Every endpoint requires ``Authorization: Bearer <AGENT_API_TOKEN>``.
- If ``AGENT_API_TOKEN`` is unset, the whole surface returns 503 (disabled).
- Destructive control actions (kill switch) additionally require an explicit
  ``{"confirm": true}`` body — a deliberate second step for an automated caller.
"""

import os
from typing import Optional

from fastapi import APIRouter, Body, Header, HTTPException

router = APIRouter(prefix="/api/agent", tags=["agent"])

_controller = None
_allow_control = False


def set_controller(controller, allow_control: bool = False) -> None:
    """Attach the live AgentController (called by the bot at startup)."""
    global _controller, _allow_control
    _controller = controller
    _allow_control = allow_control


def _require(authorization: Optional[str]) -> None:
    """Enforce token auth and a wired controller, or raise the right HTTP error."""
    token = os.getenv("AGENT_API_TOKEN")
    if not token:
        raise HTTPException(status_code=503, detail="Agent control disabled (AGENT_API_TOKEN not set)")
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")
    if _controller is None:
        raise HTTPException(status_code=503, detail="Controller not attached")


def _require_control(authorization: Optional[str]) -> None:
    """Auth + the control flag must be on for any state-changing endpoint."""
    _require(authorization)
    if not _allow_control:
        raise HTTPException(status_code=403, detail="Control disabled (agent.allow_control is false)")


def _confirm(payload: dict) -> None:
    if not (payload or {}).get("confirm"):
        raise HTTPException(status_code=400, detail='Destructive action requires {"confirm": true}')


# ---- read ----------------------------------------------------------------
@router.get("/status")
async def status(authorization: Optional[str] = Header(None, alias="Authorization")):
    _require(authorization)
    return _controller.status()


@router.get("/portfolio")
async def portfolio(authorization: Optional[str] = Header(None, alias="Authorization")):
    _require(authorization)
    return _controller.portfolio_summary()


@router.get("/risk")
async def risk(authorization: Optional[str] = Header(None, alias="Authorization")):
    _require(authorization)
    return _controller.risk_summary()


@router.get("/signals")
async def signals(limit: int = 20, authorization: Optional[str] = Header(None, alias="Authorization")):
    _require(authorization)
    return {"signals": _controller.recent_signals(limit), "accuracy": _controller.signal_accuracy()}


# ---- control (sensitive) -------------------------------------------------
@router.post("/pause")
async def pause(authorization: Optional[str] = Header(None, alias="Authorization")):
    _require_control(authorization)
    return _controller.pause()


@router.post("/resume")
async def resume(authorization: Optional[str] = Header(None, alias="Authorization")):
    _require_control(authorization)
    return _controller.resume()


@router.post("/kill-switch")
async def kill_switch(
    payload: dict = Body(default={}),
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    _require_control(authorization)
    _confirm(payload)
    return _controller.trigger_kill_switch(payload.get("reason", "Agent control API"))


@router.post("/reset-kill-switch")
async def reset_kill_switch(
    payload: dict = Body(default={}),
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    _require_control(authorization)
    _confirm(payload)
    return _controller.reset_kill_switch()
