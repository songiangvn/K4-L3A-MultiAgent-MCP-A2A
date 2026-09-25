from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from . import rules
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

COORDINATOR = "coordinator"
ORDER_AGENT = "order-agent"
PAYMENT_AGENT = "payment-agent"
SHIPMENT_AGENT = "shipment-agent"
POLICY_AGENT = "policy-agent"
VERIFIER = "verifier"

# Tool ownership: each specialist may only call the tools of its own domain.
TOOL_OWNERS = {
    ORDER_AGENT: {"get_order", "get_order_items", "get_sellers"},
    PAYMENT_AGENT: {"get_order_payments", "get_payment_timeline", "get_refund_timeline"},
    SHIPMENT_AGENT: {"get_shipment_summary"},
    POLICY_AGENT: {"get_policy"},
}

# Evidence each conclusion must cite (besides order + policy, which are always cited).
ISSUE_EVIDENCE = {
    "canceled_order_paid": ["get_payment_timeline"],
    "unavailable_order_paid": ["get_payment_timeline", "get_sellers"],
    "late_delivery_seller": ["get_shipment_summary", "get_order_items", "get_sellers"],
    "late_delivery_logistics": ["get_shipment_summary", "get_order_items"],
    "valid_split_payment": ["get_order_payments", "get_payment_timeline"],
    "payment_mismatch": ["get_order_payments", "get_payment_timeline"],
    "duplicate_charge": ["get_order_payments", "get_payment_timeline"],
    "refund_pending": ["get_payment_timeline", "get_refund_timeline"],
    "refund_failed": ["get_payment_timeline", "get_refund_timeline"],
    "unsupported_claim": ["get_shipment_summary"],
}
PAYMENT_ISSUES = {
    "valid_split_payment", "payment_mismatch", "duplicate_charge",
    "refund_pending", "refund_failed", "canceled_order_paid", "unavailable_order_paid",
}
# Pure payment-lifecycle conclusions rest on payment evidence only; the order row is context.
PAYMENT_ONLY_ISSUES: set[str] = set()  # v4 showed order evidence is required for these too
OPTIONAL_TOOLS = {"get_refund_timeline"}  # an order without refunds has no refund record
MAX_ATTEMPTS = 2


@dataclass
class Message:
    """A2A envelope exchanged between actors, correlated by case_id."""

    case_id: str
    sender: str
    recipient: str
    intent: str
    payload: dict[str, Any] = field(default_factory=dict)


class CaseContext:
    def __init__(self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case = case
        self.case_id: str = case["case_id"]
        self.order_id: str = case["customer_request"]["claimed_order_id"]
        self.gateway = gateway
        self.trace = trace
        self.evidence: dict[str, dict[str, Any]] = {}

    def send(self, message: Message, event_type: str, decision_code: str | None = None) -> None:
        self.trace.emit(
            case_id=message.case_id,
            event_type=event_type,
            actor=message.sender,
            target=message.recipient,
            decision_code=decision_code or message.intent,
        )

    async def fetch(self, actor: str, tool: str, **arguments: str) -> dict[str, Any] | None:
        if tool not in TOOL_OWNERS[actor]:
            raise PermissionError(f"{actor} is not allowed to call {tool}")
        last_error: Exception | None = None
        for _ in range(MAX_ATTEMPTS):
            try:
                evidence = await self.gateway.call(tool, case_id=self.case_id, **arguments)
                break
            except RuntimeError:  # tool-level error: not retryable
                if tool in OPTIONAL_TOOLS:
                    return None
                raise
            except (TimeoutError, OSError) as exc:  # transport error: bounded retry
                last_error = exc
                await asyncio.sleep(1)
        else:
            raise RuntimeError(f"{tool} failed after {MAX_ATTEMPTS} attempts: {last_error}")
        self.evidence[tool] = evidence
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool,
            evidence_refs=[evidence["evidence_ref"]],
            attributes={"domain": evidence["domain"]},
        )
        return evidence

    def data(self, tool: str) -> Any:
        evidence = self.evidence.get(tool)
        return None if evidence is None else evidence["data"]

    def ref(self, tool: str) -> str | None:
        evidence = self.evidence.get(tool)
        return None if evidence is None else evidence["evidence_ref"]


# --- specialists ---------------------------------------------------------------------


async def order_agent(ctx: CaseContext) -> rules.OrderFacts:
    order = await ctx.fetch(ORDER_AGENT, "get_order", order_id=ctx.order_id)
    items = await ctx.fetch(ORDER_AGENT, "get_order_items", order_id=ctx.order_id)
    await ctx.fetch(ORDER_AGENT, "get_sellers", order_id=ctx.order_id)
    assert order is not None and items is not None
    if order["data"].get("order_id") != ctx.order_id:
        raise ValueError(f"{ctx.case_id}: get_order returned a different order")
    return rules.order_facts(order["data"], items["data"], ctx.case["opened_at"])


async def payment_agent(ctx: CaseContext, facts: rules.OrderFacts) -> rules.PaymentFacts:
    await ctx.fetch(PAYMENT_AGENT, "get_order_payments", order_id=ctx.order_id)
    timeline = await ctx.fetch(PAYMENT_AGENT, "get_payment_timeline", order_id=ctx.order_id)
    refunds = await ctx.fetch(PAYMENT_AGENT, "get_refund_timeline", order_id=ctx.order_id)
    assert timeline is not None
    return rules.payment_facts(facts, timeline["data"], refunds["data"] if refunds else None)


async def shipment_agent(ctx: CaseContext, facts: rules.OrderFacts) -> rules.ShipmentFacts:
    shipment = await ctx.fetch(SHIPMENT_AGENT, "get_shipment_summary", order_id=ctx.order_id)
    assert shipment is not None
    return rules.shipment_facts(facts, shipment["data"])


async def policy_agent(ctx: CaseContext) -> dict[str, Any]:
    policy = await ctx.fetch(
        POLICY_AGENT, "get_policy", policy_version=ctx.case["policy_version"]
    )
    assert policy is not None
    return policy["data"]


# --- output assembly -----------------------------------------------------------------


def _amount(value: Decimal | float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def build_output(
    ctx: CaseContext,
    order: rules.OrderFacts,
    pay: rules.PaymentFacts,
    decision: rules.Decision,
    policy: dict[str, Any],
) -> dict[str, Any]:
    issue = decision.issue
    rule = policy["rules"][issue]
    action = rule["recommended_action"]
    refund = _amount(rule["refund_brl"])

    parties = []
    for party in rule["responsible_parties"]:
        party_id = party["party_id"]
        if party["party_type"] == "seller":
            # the policy table carries a template seller; responsibility is this order's seller
            party_id = order.seller_ids[0] if order.seller_ids else None
        parties.append({"party_type": party["party_type"], "party_id": party_id})

    base = [] if issue in PAYMENT_ONLY_ISSUES else ["get_order"]
    tools = [*base, *ISSUE_EVIDENCE[issue], "get_policy"]
    evidence_refs = list(dict.fromkeys(r for r in (ctx.ref(t) for t in tools) if r))

    payment_refs = []
    if issue in PAYMENT_ISSUES:
        payment_refs = sorted(
            {f"{ctx.order_id}:{p['payment_sequential']}" for p in pay.payment_rows}
        )

    refund_lines = []
    if refund > 0:
        refund_lines.append(
            {"reason_code": action, "amount_brl": refund, "entity_id": ctx.order_id}
        )

    claim_assessments = []
    for claim in ctx.case["customer_request"]["claims"]:
        topic = claim["topic"]
        if topic == "requested_full_refund":
            captured = _amount(pay.captured_total)
            if refund <= 0:
                verdict = "unsupported"
            elif refund >= captured:
                verdict = "supported"
            else:
                verdict = "partially_supported"
        elif topic == "unsupported_claim":
            verdict = "unsupported" if issue == "unsupported_claim" else "insufficient_evidence"
        else:
            verdict = "supported" if topic == issue else "unsupported"
        claim_assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": decision.confidence,
                "evidence_refs": evidence_refs,
            }
        )

    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": rule["case_status"],
            "confidence": decision.confidence,
        },
        "affected_entities": {
            "order_ids": [ctx.order_id],
            "item_ids": order.item_ids,
            "seller_ids": order.seller_ids,
            "payment_references": payment_refs,
            "shipment_ids": [],
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [action],
    }


def verify(ctx: CaseContext, output: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    """Pre-finalize invariants; returns the list of violated checks."""
    problems = []
    fetched = {e["evidence_ref"] for e in ctx.evidence.values()}
    if not output["evidence_refs"] or not set(output["evidence_refs"]) <= fetched:
        problems.append("EVIDENCE_NOT_OWNED")
    needs_order = output["assessment"]["primary_issue"] not in PAYMENT_ONLY_ISSUES
    if needs_order and ctx.ref("get_order") not in output["evidence_refs"]:
        problems.append("MISSING_ORDER_EVIDENCE")
    if ctx.ref("get_policy") not in output["evidence_refs"]:
        problems.append("MISSING_POLICY_EVIDENCE")
    financial = output["financial_resolution"]
    line_total = round(sum(line["amount_brl"] for line in financial["refund_lines"]), 2)
    if line_total != financial["recommended_refund_brl"]:
        problems.append("REFUND_TOTAL_MISMATCH")
    status = output["assessment"]["case_status"]
    if status == "no_action" and financial["recommended_refund_brl"] > 0:
        problems.append("NO_ACTION_WITH_REFUND")
    rule = policy["rules"][output["assessment"]["primary_issue"]]
    actions = output["resolution_actions"]
    if status != rule["case_status"] or actions != [rule["recommended_action"]]:
        problems.append("POLICY_MISMATCH")
    sellers = set(output["affected_entities"]["seller_ids"])
    for party in output["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] == "seller" and party["party_id"] not in sellers:
            problems.append("SELLER_NOT_IN_SCOPE")
    if output["affected_entities"]["order_ids"] != [ctx.order_id]:
        problems.append("ENTITY_SCOPE")
    return problems


# --- coordinator ---------------------------------------------------------------------


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    ctx = CaseContext(case, gateway, trace)
    cid = ctx.case_id

    def assign(agent: str, intent: str) -> None:
        ctx.send(Message(cid, COORDINATOR, agent, intent), "task_assigned")

    def report(agent: str, code: str) -> None:
        ctx.send(Message(cid, agent, COORDINATOR, "report", {"code": code}), "handoff", code)

    assign(ORDER_AGENT, "RESOLVE_ORDER_SCOPE")
    order = await order_agent(ctx)
    report(ORDER_AGENT, f"ORDER_{order.status.upper()}")

    assign(PAYMENT_AGENT, "VERIFY_PAYMENT_LIFECYCLE")
    assign(SHIPMENT_AGENT, "VERIFY_DELIVERY_TIMELINE")
    assign(POLICY_AGENT, "LOAD_POLICY")
    pay, ship, policy = await asyncio.gather(
        payment_agent(ctx, order), shipment_agent(ctx, order), policy_agent(ctx)
    )
    report(PAYMENT_AGENT, f"CAPTURES_{len(pay.captures)}_REFUNDS_{len(pay.refunds)}")
    delivery = "LATE_SELLER" if ship.seller_late else "LATE" if ship.late else "ON_TIME"
    report(SHIPMENT_AGENT, delivery)
    report(POLICY_AGENT, f"POLICY_{case['policy_version']}")

    decision = rules.decide(order, pay, ship)
    trace.emit(
        case_id=cid,
        event_type="policy_decided",
        actor=POLICY_AGENT,
        decision_code=decision.issue.upper(),
        evidence_refs=[ctx.ref("get_policy")],
        attributes={"candidates": ",".join(decision.signals) or "none"},
    )
    output = build_output(ctx, order, pay, decision, policy)

    ctx.send(Message(cid, COORDINATOR, VERIFIER, "VERIFY_OUTPUT"), "handoff")
    problems = verify(ctx, output, policy)
    trace.emit(
        case_id=cid,
        event_type="verification_completed",
        actor=VERIFIER,
        decision_code="PASS" if not problems else "FAIL",
        evidence_refs=output["evidence_refs"][:20],
        attributes={"problems": ",".join(problems) or "none"},
    )
    if problems:
        raise ValueError(f"{cid}: verification failed: {problems}")
    ctx.send(Message(cid, VERIFIER, COORDINATOR, "APPROVED"), "handoff")
    return output
