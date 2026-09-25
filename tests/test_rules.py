from student_agent import rules

OPENED = "2018-03-12T09:00:00-03:00"
ORDER = {
    "order_id": "o1",
    "order_status": "delivered",
    "order_purchase_timestamp": "2018-02-28T09:00:00-03:00",
    "order_approved_at": "2018-02-28T10:00:00-03:00",
    "order_delivered_carrier_date": "2018-03-02T09:00:00-03:00",
    "order_delivered_customer_date": "2018-03-09T09:00:00-03:00",
    "order_estimated_delivery_date": "2018-03-10T09:00:00-03:00",
}
ITEM = {
    "order_id": "o1", "order_item_id": "i1", "product_id": "p1", "seller_id": "s1",
    "shipping_limit_date": "2018-03-03T09:00:00-03:00", "price": "79.00", "freight_value": "10.00",
}


def cap(at: str, amount: str) -> dict:
    return {"order_id": "o1", "event_at": at, "event_type": "captured",
            "amount_brl": amount, "status": "confirmed"}


def solve(order=ORDER, items=(ITEM,), events=(), refunds=None, shipment_events=()):
    facts = rules.order_facts(order, list(items), OPENED)
    pay = rules.payment_facts(facts, {"payments": [], "events": list(events)}, refunds)
    ship = rules.shipment_facts(facts, {"events": list(shipment_events)})
    return rules.decide(facts, pay, ship).issue


def test_duplicated_rows_on_unavailable_order_are_not_a_duplicate_charge():
    order = {**ORDER, "order_status": "unavailable", "order_delivered_customer_date": None}
    same = cap("2018-02-28T10:00:00-03:00", "89.00")
    assert solve(order, items=(ITEM, ITEM), events=(same, same)) == "unavailable_order_paid"


def test_refund_failed_wins_over_overlaid_split_payment():
    events = (
        cap("2018-02-28T10:00:00-03:00", "52.00"),
        cap("2018-02-28T10:00:00-03:00", "44.50"),
        cap("2018-02-28T11:00:00-03:00", "44.50"),
    )
    refund = {"event_at": "2018-03-11T09:00:00-03:00", "event_type": "refund_requested",
              "amount_brl": "52.00", "status": "failed"}
    refunds = {"events": [refund]}
    assert solve(events=events, refunds=refunds) == "refund_failed"


def test_mismatch_from_shifted_block_is_ignored():
    events = (
        cap("2018-02-28T10:00:00-03:00", "89.00"),
        cap("2018-03-05T10:00:00-03:00", "35.00"),
        {"event_at": "2018-03-05T12:00:00-03:00", "event_type": "reconciliation_mismatch",
         "amount_brl": "35.00", "status": "open"},
    )
    assert solve(events=events) == "unsupported_claim"


def test_split_versus_duplicate_uses_order_total():
    split = (cap("2018-02-28T10:00:00-03:00", "44.50"), cap("2018-02-28T11:00:00-03:00", "44.50"))
    dup = (cap("2018-02-28T10:00:00-03:00", "64.00"), cap("2018-02-28T11:00:00-03:00", "64.00"))
    assert solve(events=split) == "valid_split_payment"
    assert solve(events=dup) == "duplicate_charge"


def test_late_delivery_responsibility_follows_shipping_limit():
    late = {**ORDER, "order_delivered_customer_date": "2018-03-11T09:00:00-03:00"}
    assert solve(late) == "late_delivery_logistics"
    seller_late = {**late, "order_delivered_carrier_date": "2018-03-04T09:00:00-03:00"}
    assert solve(seller_late) == "late_delivery_seller"


def test_out_of_window_late_event_does_not_create_an_issue():
    event = {"event_at": "2018-05-17T09:00:00-03:00", "event_type": "delivered_late",
             "actor": "logistics_provider", "status": "confirmed"}
    assert solve(shipment_events=(event,)) == "unsupported_claim"
