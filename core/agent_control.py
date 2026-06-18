"""
Agent Control
=============

A thin façade exposing read + control operations over the running bot, intended
to be driven by an EXTERNAL agent (e.g. the user's OpenClaw agent) through the
HTTP control API in ``dashboard/agent_api.py``.

The brain lives outside this repo. This module just provides safe, well-scoped
operations and holds the ``paused`` flag the trading loop checks. All methods are
pure Python (no HTTP), so they're unit-testable on their own.

Control operations are sensitive (they can halt trading), so the HTTP layer
gates them behind a bearer token + explicit confirmation — see agent_api.py.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class AgentController:
    """Read/control façade over the bot's components for an external agent."""

    def __init__(
        self,
        *,
        portfolio=None,
        risk_manager=None,
        execution_engine=None,
        signal_db=None,
        dashboard=None,
        mode: str = "dry_run",
    ):
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.execution_engine = execution_engine
        self.signal_db = signal_db
        self.dashboard = dashboard
        self.mode = mode
        self._paused = False

    # ---- read -------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return self._paused

    def status(self) -> dict:
        risk = self.risk_manager.get_summary() if self.risk_manager else {}
        return {
            "mode": self.mode,
            "paused": self._paused,
            "kill_switch_triggered": risk.get("kill_switch_triggered", False),
            "kill_switch_reason": risk.get("kill_switch_reason", ""),
            "open_orders": (
                self.execution_engine.open_order_count if self.execution_engine else 0
            ),
        }

    def portfolio_summary(self) -> dict:
        return self.portfolio.get_summary() if self.portfolio else {}

    def risk_summary(self) -> dict:
        return self.risk_manager.get_summary() if self.risk_manager else {}

    def recent_signals(self, limit: int = 20) -> list:
        if self.dashboard is None:
            return []
        return list(getattr(self.dashboard, "ai_signals", [])[-limit:])

    def signal_accuracy(self, min_confidence: float = 0.65, lookback_days: int = 30) -> dict:
        if self.signal_db is None:
            return {"error": "signal database disabled"}
        return self.signal_db.get_signal_accuracy(
            min_confidence=min_confidence, lookback_days=lookback_days
        )

    # ---- control (sensitive) ---------------------------------------------
    def pause(self) -> dict:
        """Stop submitting new orders. Existing orders are unaffected."""
        self._paused = True
        logger.warning("[AgentControl] Trading PAUSED via control API")
        return self.status()

    def resume(self) -> dict:
        self._paused = False
        logger.warning("[AgentControl] Trading RESUMED via control API")
        return self.status()

    def trigger_kill_switch(self, reason: str = "Agent control API") -> dict:
        if self.risk_manager is not None:
            self.risk_manager.trigger_kill_switch(reason)
        return self.status()

    def reset_kill_switch(self) -> dict:
        if self.risk_manager is not None:
            self.risk_manager.reset_kill_switch()
        return self.status()
