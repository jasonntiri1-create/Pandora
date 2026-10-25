"""
P2 — Universal Action Gateway.

Single chokepoint for every consequential action in Pandora. Pipeline:

    Intent → Risk Assessment → Policy Engine → Budget Check →
    Approval Evaluation → Execution → Audit Logging

Every callable side-effect (financial, drone, device, voice command, planner step,
dashboard action, external API call) MUST submit an ActionEnvelope and receive an
ActionResult. No direct execution path is permitted.

The gateway itself is dependency-injected: callers pass adapters for risk, policy,
budget, approval, audit, and event publishing. Adapters get null defaults so the
gateway can run in tests / partial-init environments. A `contextvars` flag is used
to mark code that is currently inside a gateway dispatch, enabling the
`@gateway_only` decorator to reject direct bypass calls.
"""

from __future__ import annotations

import asyncio
import contextvars
import fnmatch
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- types


class ActionSource(StrEnum):
    VOICE = "voice"
    DASHBOARD = "dashboard"
    SWARM = "swarm"
    PLANNER = "planner"
    FINANCIAL = "financial"
    DRONE = "drone"
    EXTERNAL_API = "external_api"
    SYSTEM = "system"
    WORKFLOW = "workflow"


class ActionStatus(StrEnum):
    EXECUTED = "executed"
    DENIED = "denied"
    REJECTED_APPROVAL = "rejected_approval"
    BUDGET_EXCEEDED = "budget_exceeded"
    NO_HANDLER = "no_handler"
    ERROR = "error"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class ActionEnvelope:
    intent: str  # canonical verb e.g. "financial.trade.buy"
    source: ActionSource
    actor: str
    payload: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    parent_intent_id: str | None = None  # Wave 3C: Intent lineage tracking


@dataclass
class RiskAssessment:
    score: float  # 0.0–1.0
    level: RiskLevel
    reasons: list[str] = field(default_factory=list)


@dataclass
class PolicyEvaluation:
    decision: PolicyDecision
    reason: str = ""


@dataclass
class BudgetCheck:
    allowed: bool
    reason: str = ""


@dataclass
class ApprovalOutcome:
    approved: bool
    approver: str | None = None
    reason: str = ""


@dataclass
class ActionResult:
    envelope_id: str
    status: ActionStatus
    output: Any = None
    error: str | None = None
    risk: RiskAssessment | None = None
    policy: PolicyEvaluation | None = None
    budget: BudgetCheck | None = None
    approval: ApprovalOutcome | None = None
    latency_ms: float = 0.0
    parent_intent_id: str | None = None  # Wave 3C: Intent lineage tracking
    lineage_depth: int = 0  # Wave 3C: Depth in intent chain


# Adapter signatures (kept loose so existing modules can plug in).
RiskFn = Callable[[ActionEnvelope], RiskAssessment]
PolicyFn = Callable[[ActionEnvelope, RiskAssessment], PolicyEvaluation]
BudgetFn = Callable[[ActionEnvelope], BudgetCheck]
ApprovalFn = Callable[[ActionEnvelope, RiskAssessment], Awaitable[ApprovalOutcome]]
AuditFn = Callable[[ActionEnvelope, ActionResult], None]
EventFn = Callable[[str, dict[str, Any]], None]
HandlerFn = Callable[[ActionEnvelope], Awaitable[Any]]


# --------------------------------------------------------------------------- defaults


def _default_risk(envelope: ActionEnvelope) -> RiskAssessment:
    """Classify known high-risk intents and fail closed for unclassified intents."""
    high_prefixes = (
        "financial.",
        "drone.",
        "cybersecurity.",
        "device.write",
        "system.",
        "exec.",
        "shell.",
        "network.",
    )
    if any(envelope.intent.startswith(prefix) for prefix in high_prefixes):
        return RiskAssessment(0.9, RiskLevel.HIGH, ["default high-risk intent prefix"])

    read_only_prefixes = ("noop.", "read.", "query.", "status.", "health.")
    if any(envelope.intent.startswith(prefix) for prefix in read_only_prefixes):
        return RiskAssessment(0.1, RiskLevel.LOW, ["known read-only intent prefix"])

    return RiskAssessment(
        0.5,
        RiskLevel.MEDIUM,
        ["unclassified intent requires an explicit risk policy"],
    )


def _default_policy(envelope: ActionEnvelope, risk: RiskAssessment) -> PolicyEvaluation:
    if risk.level == RiskLevel.CRITICAL:
        return PolicyEvaluation(PolicyDecision.DENY, "critical risk denied by default policy")
    if risk.level in (RiskLevel.MEDIUM, RiskLevel.HIGH):
        return PolicyEvaluation(
            PolicyDecision.REQUIRE_APPROVAL,
            f"{risk.level.value} risk requires approval",
        )
    return PolicyEvaluation(PolicyDecision.ALLOW)


def _default_budget(envelope: ActionEnvelope) -> BudgetCheck:
    return BudgetCheck(allowed=True)


async def _default_approval(envelope: ActionEnvelope, risk: RiskAssessment) -> ApprovalOutcome:
    # Without an explicit approval workflow wired, REJECT high-risk actions —
    # fail-closed is the only safe default.
    return ApprovalOutcome(
        approved=False,
        approver=None,
        reason="No approval workflow configured; rejecting fail-closed",
    )


def _default_audit(envelope: ActionEnvelope, result: ActionResult) -> None:
    logger.info(
        "audit envelope_id=%s intent=%s source=%s actor=%s status=%s latency_ms=%.1f",
        envelope.id,
        envelope.intent,
        envelope.source.value,
        envelope.actor,
        result.status.value,
        result.latency_ms,
    )


def _default_event(topic: str, payload: dict[str, Any]) -> None:
    logger.debug("event %s %s", topic, payload)


# --------------------------------------------------------------------------- gateway


_IN_DISPATCH: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "pandora_action_gateway_in_dispatch", default=False
)


@dataclass
class HandlerHandle:
    """Returned by register_handler. Call .unregister() to deregister (used by lazy capabilities on deactivation)."""

    _gateway: "ActionGateway"
    _entry: tuple[str, HandlerFn]
    _alive: bool = True

    def unregister(self) -> None:
        if not self._alive:
            return
        self._gateway._remove_handler_entry(self._entry)
        self._alive = False


class IntentBuilder:
    """Normalised constructors for ActionEnvelopes by origin. Use these instead of building envelopes inline."""

    @staticmethod
    def _build(
        source: ActionSource,
        intent: str,
        actor: str,
        payload: dict | None,
        context: dict | None,
        parent_intent_id: str | None = None,
    ) -> ActionEnvelope:
        return ActionEnvelope(
            intent=intent,
            source=source,
            actor=actor,
            payload=payload or {},
            context=context or {},
            parent_intent_id=parent_intent_id,
        )

    @staticmethod
    def from_voice(
        intent: str,
        actor: str = "voice",
        *,
        payload=None,
        context=None,
        parent_intent_id: str | None = None,
    ) -> ActionEnvelope:
        return IntentBuilder._build(
            ActionSource.VOICE, intent, actor, payload, context, parent_intent_id
        )

    @staticmethod
    def from_dashboard(
        intent: str,
        actor: str = "dashboard",
        *,
        payload=None,
        context=None,
        parent_intent_id: str | None = None,
    ) -> ActionEnvelope:
        return IntentBuilder._build(
            ActionSource.DASHBOARD, intent, actor, payload, context, parent_intent_id
        )

    @staticmethod
    def from_planner(
        intent: str,
        actor: str = "planner",
        *,
        payload=None,
        context=None,
        parent_intent_id: str | None = None,
    ) -> ActionEnvelope:
        return IntentBuilder._build(
            ActionSource.PLANNER, intent, actor, payload, context, parent_intent_id
        )

    @staticmethod
    def from_swarm(
        intent: str,
        actor: str = "swarm",
        *,
        payload=None,
        context=None,
        parent_intent_id: str | None = None,
    ) -> ActionEnvelope:
        return IntentBuilder._build(
            ActionSource.SWARM, intent, actor, payload, context, parent_intent_id
        )

    @staticmethod
    def from_financial(
        intent: str,
        actor: str = "financial",
        *,
        payload=None,
        context=None,
        parent_intent_id: str | None = None,
    ) -> ActionEnvelope:
        return IntentBuilder._build(
            ActionSource.FINANCIAL, intent, actor, payload, context, parent_intent_id
        )

    @staticmethod
    def from_device(
        intent: str,
        actor: str = "device",
        *,
        payload=None,
        context=None,
        parent_intent_id: str | None = None,
    ) -> ActionEnvelope:
        return IntentBuilder._build(
            ActionSource.DRONE if intent.startswith("drone.") else ActionSource.SYSTEM,
            intent,
            actor,
            payload,
            context,
            parent_intent_id,
        )

    @staticmethod
    def from_workflow(
        intent: str,
        actor: str = "workflow",
        *,
        payload=None,
        context=None,
        parent_intent_id: str | None = None,
    ) -> ActionEnvelope:
        return IntentBuilder._build(
            ActionSource.WORKFLOW, intent, actor, payload, context, parent_intent_id
        )


class ActionGateway:
    """The universal action gateway."""

    def __init__(
        self,
        *,
        risk: RiskFn | None = None,
        policy: PolicyFn | None = None,
        budget: BudgetFn | None = None,
        approval: ApprovalFn | None = None,
        audit: AuditFn | None = None,
        event: EventFn | None = None,
    ) -> None:
        self._risk = risk or _default_risk
        self._policy = policy or _default_policy
        self._budget = budget or _default_budget
        self._approval = approval or _default_approval
        self._audit = audit or _default_audit
        self._event = event or _default_event
        self._handlers: list[tuple[str, HandlerFn]] = []
        self._metrics: dict[str, Any] = {
            "actions_executed": 0,
            "actions_denied": 0,
            "actions_awaiting_approval": 0,
            "actions_rejected_approval": 0,
            "actions_budget_exceeded": 0,
            "actions_no_handler": 0,
            "actions_failed": 0,
            "gateway_latency_ms_sum": 0.0,
            "gateway_latency_ms_count": 0,
            "risk_distribution": {lvl.value: 0 for lvl in RiskLevel},
            "approval_distribution": {"approved": 0, "rejected": 0},
        }

    # ---- handler registration --------------------------------------------

    def register_handler(self, intent_pattern: str, handler: HandlerFn) -> HandlerHandle:
        """Register an async handler. Patterns use fnmatch (e.g. 'financial.trade.*'). Returns a HandlerHandle."""
        if not asyncio.iscoroutinefunction(handler):
            raise TypeError(f"Handler for {intent_pattern!r} must be an async function")
        entry = (intent_pattern, handler)
        self._handlers.append(entry)
        return HandlerHandle(_gateway=self, _entry=entry)

    def _remove_handler_entry(self, entry: tuple[str, HandlerFn]) -> None:
        try:
            self._handlers.remove(entry)
        except ValueError:
            pass

    def _resolve_handler(self, intent: str) -> HandlerFn | None:
        for pattern, handler in self._handlers:
            if fnmatch.fnmatchcase(intent, pattern):
                return handler
        return None

    # ---- metrics ---------------------------------------------------------

    def metrics_snapshot(self) -> dict[str, Any]:
        m = self._metrics
        avg = (
            (m["gateway_latency_ms_sum"] / m["gateway_latency_ms_count"])
            if m["gateway_latency_ms_count"]
            else 0.0
        )
        return {
            **{k: v for k, v in m.items() if not k.startswith("gateway_latency_ms_")},
            "gateway_latency_ms_avg": avg,
            "gateway_latency_ms_count": m["gateway_latency_ms_count"],
            "risk_distribution": dict(m["risk_distribution"]),
            "approval_distribution": dict(m["approval_distribution"]),
        }

    def _record_metrics(self, result: ActionResult) -> None:
        m = self._metrics
        m["gateway_latency_ms_sum"] += result.latency_ms
        m["gateway_latency_ms_count"] += 1
        if result.risk is not None:
            m["risk_distribution"][result.risk.level.value] += 1
        if result.approval is not None:
            key = "approved" if result.approval.approved else "rejected"
            m["approval_distribution"][key] += 1
        status_map = {
            ActionStatus.EXECUTED: "actions_executed",
            ActionStatus.DENIED: "actions_denied",
            ActionStatus.REJECTED_APPROVAL: "actions_rejected_approval",
            ActionStatus.BUDGET_EXCEEDED: "actions_budget_exceeded",
            ActionStatus.NO_HANDLER: "actions_no_handler",
            ActionStatus.ERROR: "actions_failed",
        }
        key = status_map.get(result.status)
        if key:
            m[key] += 1

    # ---- dispatch --------------------------------------------------------

    async def submit(self, envelope: ActionEnvelope) -> ActionResult:
        t0 = time.monotonic()
        # Wave 3C: Calculate lineage depth
        lineage_depth = 0
        if envelope.parent_intent_id:
            # In a full implementation, we would look up the parent's lineage depth
            # For now, we'll use a simple heuristic: increment depth if parent exists
            lineage_depth = 1

        result = ActionResult(
            envelope_id=envelope.id,
            status=ActionStatus.ERROR,
            parent_intent_id=envelope.parent_intent_id,
            lineage_depth=lineage_depth,
        )

        try:
            # 1. Risk
            risk = self._risk(envelope)
            result.risk = risk

            # 2. Policy
            policy = self._policy(envelope, risk)
            result.policy = policy
            if policy.decision == PolicyDecision.DENY:
                result.status = ActionStatus.DENIED
                result.error = policy.reason
                return result

            # 3. Budget
            budget = self._budget(envelope)
            result.budget = budget
            if not budget.allowed:
                result.status = ActionStatus.BUDGET_EXCEEDED
                result.error = budget.reason
                return result

            # 4. Approval: caller-supplied context can never bypass this gate.
            if policy.decision == PolicyDecision.REQUIRE_APPROVAL or risk.level in (
                RiskLevel.MEDIUM,
                RiskLevel.HIGH,
                RiskLevel.CRITICAL,
            ):
                self._metrics["actions_awaiting_approval"] += 1
                try:
                    approval = await self._approval(envelope, risk)
                finally:
                    self._metrics["actions_awaiting_approval"] -= 1
                result.approval = approval
                if not approval.approved:
                    result.status = ActionStatus.REJECTED_APPROVAL
                    result.error = approval.reason
                    return result

            # 5. Execute
            handler = self._resolve_handler(envelope.intent)
            if handler is None:
                result.status = ActionStatus.NO_HANDLER
                result.error = f"No handler registered for intent {envelope.intent!r}"
                return result

            token = _IN_DISPATCH.set(True)
            try:
                result.output = await handler(envelope)
            finally:
                _IN_DISPATCH.reset(token)
            result.status = ActionStatus.EXECUTED
            result.error = None
            return result

        except Exception as e:  # noqa: BLE001 — gateway must never raise to caller
            result.status = ActionStatus.ERROR
            result.error = f"{type(e).__name__}: {e}"
            logger.exception("Gateway dispatch failed for %s", envelope.intent)
            return result
        finally:
            result.latency_ms = (time.monotonic() - t0) * 1000
            self._record_metrics(result)
            # 6. Audit + event — these MUST run, even on failure.
            try:
                self._audit(envelope, result)
            except Exception:  # noqa: BLE001
                logger.exception("Audit sink raised")
            try:
                self._event(
                    "action.completed",
                    {
                        "envelope_id": envelope.id,
                        "intent": envelope.intent,
                        "source": envelope.source.value,
                        "status": result.status.value,
                        "latency_ms": result.latency_ms,
                    },
                )
            except Exception:  # noqa: BLE001
                logger.exception("Event sink raised")


# --------------------------------------------------------------------------- bypass guard


class GatewayBypassError(RuntimeError):
    """Raised when a @gateway_only function is called outside a dispatch context."""


def gateway_only(fn: Callable) -> Callable:
    """
    Decorator: marks a side-effecting function as callable ONLY from inside a
    gateway dispatch. Use on the raw implementations of handlers to prevent
    accidental direct invocation from elsewhere in the codebase.
    """

    if asyncio.iscoroutinefunction(fn):

        @wraps(fn)
        async def _async(*args: Any, **kwargs: Any) -> Any:
            if not _IN_DISPATCH.get():
                raise GatewayBypassError(
                    f"{fn.__qualname__} can only be invoked via ActionGateway.submit()"
                )
            return await fn(*args, **kwargs)

        return _async

    @wraps(fn)
    def _sync(*args: Any, **kwargs: Any) -> Any:
        if not _IN_DISPATCH.get():
            raise GatewayBypassError(
                f"{fn.__qualname__} can only be invoked via ActionGateway.submit()"
            )
        return fn(*args, **kwargs)

    return _sync
