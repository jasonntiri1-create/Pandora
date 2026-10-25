from __future__ import annotations


import pytest

from pandora.runtime.action_gateway import (
    ActionEnvelope,
    ActionGateway,
    ActionSource,
    ActionStatus,
    ApprovalOutcome,
    BudgetCheck,
    GatewayBypassError,
    PolicyDecision,
    PolicyEvaluation,
    RiskAssessment,
    RiskLevel,
    gateway_only,
)


def _env(intent: str, **kw) -> ActionEnvelope:
    return ActionEnvelope(
        intent=intent,
        source=kw.pop("source", ActionSource.SYSTEM),
        actor=kw.pop("actor", "test"),
        payload=kw.pop("payload", {}),
        context=kw.pop("context", {}),
    )


async def _ok_handler(envelope):
    return {"echo": envelope.payload}


def test_handler_must_be_async():
    gw = ActionGateway()
    with pytest.raises(TypeError):
        gw.register_handler("x.*", lambda e: None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_executed_low_risk():
    audits, events = [], []
    gw = ActionGateway(
        risk=lambda e: RiskAssessment(0.1, RiskLevel.LOW),
        audit=lambda e, r: audits.append(r.status),
        event=lambda t, p: events.append(t),
    )
    gw.register_handler("noop.*", _ok_handler)
    result = await gw.submit(_env("noop.ping", payload={"v": 1}))
    assert result.status == ActionStatus.EXECUTED
    assert result.output == {"echo": {"v": 1}}
    assert audits == [ActionStatus.EXECUTED]
    assert events == ["action.completed"]


@pytest.mark.asyncio
async def test_policy_deny():
    gw = ActionGateway(
        policy=lambda e, r: PolicyEvaluation(PolicyDecision.DENY, "forbidden"),
    )
    gw.register_handler("x.*", _ok_handler)
    result = await gw.submit(_env("x.do"))
    assert result.status == ActionStatus.DENIED
    assert result.error == "forbidden"


@pytest.mark.asyncio
async def test_budget_blocks():
    gw = ActionGateway(
        risk=lambda e: RiskAssessment(0.1, RiskLevel.LOW),
        budget=lambda e: BudgetCheck(allowed=False, reason="over quota"),
    )
    gw.register_handler("x.*", _ok_handler)
    result = await gw.submit(_env("x.do"))
    assert result.status == ActionStatus.BUDGET_EXCEEDED


@pytest.mark.asyncio
async def test_high_risk_requires_approval_rejected_by_default():
    gw = ActionGateway()  # default approval fails closed
    gw.register_handler("financial.*", _ok_handler)
    result = await gw.submit(_env("financial.trade.buy"))
    assert result.status == ActionStatus.REJECTED_APPROVAL


@pytest.mark.asyncio
async def test_approval_granted_executes():
    async def approve(env, risk):
        return ApprovalOutcome(approved=True, approver="alice")

    gw = ActionGateway(approval=approve)
    gw.register_handler("drone.*", _ok_handler)
    result = await gw.submit(_env("device.write.test"))
    assert result.status == ActionStatus.EXECUTED
    assert result.approval and result.approval.approved


@pytest.mark.asyncio
async def test_no_handler():
    gw = ActionGateway(risk=lambda e: RiskAssessment(0.1, RiskLevel.LOW))
    result = await gw.submit(_env("nothing.here"))
    assert result.status == ActionStatus.NO_HANDLER


@pytest.mark.asyncio
async def test_handler_exception_audited():
    audits = []

    async def boom(e):
        raise RuntimeError("kaboom")

    gw = ActionGateway(
        risk=lambda e: RiskAssessment(0.1, RiskLevel.LOW),
        audit=lambda e, r: audits.append((r.status, r.error)),
    )
    gw.register_handler("x.*", boom)
    result = await gw.submit(_env("x.boom"))
    assert result.status == ActionStatus.ERROR
    assert "kaboom" in (result.error or "")
    assert audits and audits[0][0] == ActionStatus.ERROR


@pytest.mark.asyncio
async def test_gateway_only_blocks_direct_call():
    @gateway_only
    async def protected():
        return "ok"

    with pytest.raises(GatewayBypassError):
        await protected()

    gw = ActionGateway(risk=lambda e: RiskAssessment(0.1, RiskLevel.LOW))

    async def handler(e):
        return await protected()

    gw.register_handler("x.*", handler)
    result = await gw.submit(_env("x.go"))
    assert result.status == ActionStatus.EXECUTED
    assert result.output == "ok"


@pytest.mark.asyncio
async def test_unclassified_intent_fails_closed_by_default():
    gw = ActionGateway()
    gw.register_handler("unknown.*", _ok_handler)
    result = await gw.submit(_env("unknown.perform"))
    assert result.status == ActionStatus.REJECTED_APPROVAL
    assert result.approval is not None
    assert result.approval.approved is False


@pytest.mark.asyncio
async def test_caller_context_cannot_bypass_high_risk_approval():
    gw = ActionGateway()
    gw.register_handler("financial.*", _ok_handler)
    result = await gw.submit(
        _env("financial.trade.buy", context={"_emergency": True, "_emergency_source": "test"})
    )
    assert result.status == ActionStatus.REJECTED_APPROVAL
    assert result.approval is not None
    assert result.approval.approved is False
