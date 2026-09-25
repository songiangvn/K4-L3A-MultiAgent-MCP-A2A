"""Deterministic L3A decision rules.

Pure functions over MCP evidence payloads (no I/O) so they can be tested offline.
Every order mixes the real scenario with distractor rows; only rows anchored to the
order's own timeline are trusted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

HOUR = timedelta(hours=1)
CAPTURE_WINDOW = 2 * HOUR  # split payments capture up to 1h after approval
FOLLOW_UP_WINDOW = 3 * HOUR  # reconciliation events follow their capture within hours

# Most specific signal first: distractor overlays tend to fake the weaker payment patterns.
ISSUE_PRIORITY = (
    "refund_failed",
    "refund_pending",
    "payment_mismatch",
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "duplicate_charge",
    "valid_split_payment",
)


def ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def money(value: Any) -> Decimal:
    return Decimal(str(value))


def dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for row in rows:
        key = json.dumps(row, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


@dataclass
class OrderFacts:
    order_id: str
    status: str
    purchase: datetime
    approved: datetime
    carrier: datetime | None
    delivered: datetime | None
    estimated: datetime | None
    opened: datetime
    items: list[dict[str, Any]]
    excluded_items: int

    @property
    def order_total(self) -> Decimal:
        return sum((money(i["price"]) + money(i["freight_value"]) for i in self.items), Decimal(0))

    @property
    def seller_ids(self) -> list[str]:
        return sorted({i["seller_id"] for i in self.items})

    @property
    def item_ids(self) -> list[str]:
        return sorted({i["order_item_id"] for i in self.items})


@dataclass
class PaymentFacts:
    captures: list[dict[str, Any]]
    mismatches: list[dict[str, Any]]
    refunds: list[dict[str, Any]]
    payment_rows: list[dict[str, Any]]
    excluded_events: int
    excluded_refunds: int

    @property
    def captured_total(self) -> Decimal:
        return sum((money(e["amount_brl"]) for e in self.captures), Decimal(0))


@dataclass
class ShipmentFacts:
    late: bool
    seller_late: bool
    event_actors: list[str]
    excluded_events: int


@dataclass
class Decision:
    issue: str
    confidence: float
    signals: list[str] = field(default_factory=list)


def order_facts(order: dict[str, Any], items: list[dict[str, Any]], opened_at: str) -> OrderFacts:
    purchase = ts(order["order_purchase_timestamp"])
    opened = ts(opened_at)
    assert purchase is not None and opened is not None
    in_window = [i for i in items if purchase <= ts(i["shipping_limit_date"]) <= opened]
    unique = dedupe(in_window)
    if unique:
        # the real shipping limit is the one closest to purchase
        first = min(ts(i["shipping_limit_date"]) for i in unique)
        unique = [i for i in unique if ts(i["shipping_limit_date"]) == first]
    return OrderFacts(
        order_id=order["order_id"],
        status=order["order_status"],
        purchase=purchase,
        approved=ts(order["order_approved_at"]) or purchase,
        carrier=ts(order["order_delivered_carrier_date"]),
        delivered=ts(order["order_delivered_customer_date"]),
        estimated=ts(order["order_estimated_delivery_date"]),
        opened=opened,
        items=unique,
        excluded_items=len(items) - len(unique),
    )


def payment_facts(
    facts: OrderFacts,
    timeline: dict[str, Any],
    refund_timeline: dict[str, Any] | None,
) -> PaymentFacts:
    events = dedupe(timeline.get("events", []))
    captures = [
        e
        for e in events
        if e["event_type"] == "captured"
        and facts.approved <= ts(e["event_at"]) <= facts.approved + CAPTURE_WINDOW
    ]
    last_capture = max((ts(e["event_at"]) for e in captures), default=facts.approved)
    mismatches = [
        e
        for e in events
        if e["event_type"] == "reconciliation_mismatch"
        and facts.approved <= ts(e["event_at"]) <= last_capture + FOLLOW_UP_WINDOW
    ]
    raw_refunds = dedupe((refund_timeline or {}).get("events", []))
    refunds = [e for e in raw_refunds if facts.purchase <= ts(e["event_at"]) <= facts.opened]
    captured_amounts = {money(e["amount_brl"]) for e in captures}
    payment_rows = [
        p for p in dedupe(timeline.get("payments", []))
        if money(p["payment_value"]) in captured_amounts
    ]
    return PaymentFacts(
        captures=captures,
        mismatches=mismatches,
        refunds=refunds,
        payment_rows=payment_rows,
        excluded_events=len(timeline.get("events", [])) - len(captures) - len(mismatches),
        excluded_refunds=len((refund_timeline or {}).get("events", [])) - len(refunds),
    )


def shipment_facts(facts: OrderFacts, shipment: dict[str, Any]) -> ShipmentFacts:
    late = bool(facts.delivered and facts.estimated and facts.delivered > facts.estimated)
    limit = min((ts(i["shipping_limit_date"]) for i in facts.items), default=None)
    seller_late = bool(late and facts.carrier and limit and facts.carrier > limit)
    anchored = [
        e for e in shipment.get("events", [])
        if facts.delivered is not None and ts(e["event_at"]) == facts.delivered
    ]
    return ShipmentFacts(
        late=late,
        seller_late=seller_late,
        event_actors=sorted({e.get("actor", "") for e in anchored}),
        excluded_events=len(shipment.get("events", [])) - len(anchored),
    )


def decide(order: OrderFacts, pay: PaymentFacts, ship: ShipmentFacts | None) -> Decision:
    signals: set[str] = set()
    for refund in pay.refunds:
        if refund["status"] == "failed":
            signals.add("refund_failed")
        elif refund["status"] == "pending":
            signals.add("refund_pending")
    if pay.mismatches:
        signals.add("payment_mismatch")
    if pay.captures and order.status == "canceled":
        signals.add("canceled_order_paid")
    if pay.captures and order.status == "unavailable":
        signals.add("unavailable_order_paid")
    if ship is not None and ship.late:
        signals.add("late_delivery_seller" if ship.seller_late else "late_delivery_logistics")
    distinct_amounts = {money(e["amount_brl"]) for e in pay.captures}
    if len(pay.captures) >= 2 and order.status not in ("canceled", "unavailable"):
        if pay.captured_total == order.order_total:
            signals.add("valid_split_payment")
        elif len(distinct_amounts) == 1 and pay.captured_total > order.order_total:
            signals.add("duplicate_charge")

    ordered = [issue for issue in ISSUE_PRIORITY if issue in signals]
    if not ordered:
        return Decision("unsupported_claim", 0.98, [])
    confidence = 0.98
    if len(ordered) > 1:
        confidence = 0.95
    if ordered[0].startswith("late_delivery") and ship is not None and ship.event_actors:
        expected = "seller" if ship.seller_late else "logistics_provider"
        if ship.event_actors != [expected]:
            confidence = 0.8
    return Decision(ordered[0], confidence, ordered)
