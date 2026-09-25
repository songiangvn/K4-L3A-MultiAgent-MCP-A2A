# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Workflow hoàn toàn deterministic (không dùng LLM): mỗi case đi qua coordinator → các specialist gọi MCP theo domain của mình → policy agent chốt issue → verifier kiểm tra invariant → output.

```text
inputs/<case_id>.json
   │
   ▼
Coordinator ──task_assigned──► Order agent ──(get_order, get_order_items, get_sellers)
   │                              │ handoff ORDER_<STATUS>
   ├──task_assigned──► Payment agent ──(get_order_payments, get_payment_timeline, get_refund_timeline)
   ├──task_assigned──► Shipment agent ──(get_shipment_summary)
   ├──task_assigned──► Policy agent ──(get_policy)
   │                              │ handoff (facts)          ─┐ chạy song song
   ▼                                                         ─┘
rules.decide() ──policy_decided──► build_output()
   │
   ▼ handoff VERIFY_OUTPUT
Verifier ──verification_completed (PASS/FAIL)──► handoff APPROVED ──► outputs/<case_id>.json
                                                   mọi bước ghi traces/trace.jsonl
```

Code: `src/student_agent/workflow.py` (agents, A2A, verifier), `src/student_agent/rules.py` (luật thuần, test offline được).

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | case input | Giao việc, gom facts, gọi rules, chuyển verifier | `task_assigned`, `handoff` tới verifier |
| Order/item | `claimed_order_id` | Lấy order, item, seller; lọc item nằm ngoài timeline, bỏ dòng trùng | `OrderFacts`, handoff `ORDER_<STATUS>` |
| Payment | OrderFacts | Chỉ giữ capture neo vào `order_approved_at` (≤2h), mismatch theo capture thật (≤3h), refund trong [purchase, opened_at] | `PaymentFacts`, handoff `CAPTURES_n_REFUNDS_m` |
| Shipment | OrderFacts | Trễ = delivered > estimated; seller trễ nếu carrier date > shipping limit; event chỉ tin khi trùng ngày giao | `ShipmentFacts`, handoff `ON_TIME/LATE/LATE_SELLER` |
| Policy | `policy_version` | Lấy policy; chốt issue theo thứ tự ưu tiên; tra status/action/refund/party | `policy_decided` với decision code = issue |
| Verifier | output + evidence đã lấy | Kiểm tra invariant (mục 6) | `verification_completed` PASS/FAIL |

Quyền tool được enforce trong `TOOL_OWNERS`; agent gọi tool ngoài domain sẽ bị `PermissionError`. Không agent nào gọi `get_customer_history` hay `get_product_context` (không cần cho kết luận).

## 3. A2A protocol

- Envelope `Message(case_id, sender, recipient, intent, payload)`; mọi message correlate theo `case_id`.
- Mỗi message được trace thành `task_assigned` (coordinator → specialist) hoặc `handoff` (specialist → coordinator, coordinator ↔ verifier) với `decision_code` là mã quan sát được.
- Luồng một chiều cố định: order → (payment ∥ shipment ∥ policy) → decide → verifier. Không có vòng lặp nên không cần phát hiện loop; verifier FAIL làm dừng run (không tự sửa).
- Timeout transport do MCP client đảm nhận (300s read, 30s connect).

## 4. Evidence lifecycle

1. `EvidenceGateway.call` validate envelope theo `mcp-evidence-response-v1`.
2. `CaseContext.fetch` lưu evidence theo tool trong phạm vi một case (không cache giữa case) và emit `tool_result_consumed` với `evidence_ref` + domain.
3. Output chỉ cite evidence thật sự hỗ trợ kết luận: luôn `get_policy` + `get_order`, cộng các tool theo issue (`ISSUE_EVIDENCE`). Không bao giờ cite domain product/customer.
4. Verifier xác nhận mọi ref trong output đều nằm trong tập evidence đã fetch của chính case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | Có, tối đa 2 lần (idempotent read) | Dừng run, không đoán | exception, không emit consumed |
| Not found | Không | `get_refund_timeline`: hiểu là không có refund; tool khác: dừng run | không emit consumed cho tool đó |
| Source conflict | Không | Lọc theo timeline của order; dòng trùng bị dedupe; nhiều tín hiệu → chọn theo ưu tiên, confidence 0.9 | `policy_decided.attributes.candidates` |
| Invalid specialist result | Không | Verifier FAIL → raise, không ghi output | `verification_completed` FAIL |

## 6. Verification invariants

- Output pass JSON Schema (CLI validate trước khi ghi file).
- `order_ids == [claimed_order_id]`; seller trong responsible parties thuộc `seller_ids` của order.
- Mọi `evidence_ref` thuộc evidence đã fetch trong case; luôn có policy evidence + order evidence.
- Tổng `refund_lines` = `recommended_refund_brl`; `no_action` ⇒ refund 0.
- `case_status` và `resolution_actions` khớp policy rule của issue.
- Confidence trong [0, 1]: 0.95 khi một tín hiệu duy nhất, 0.9 khi phải chọn theo ưu tiên, 0.75 khi actor shipment mâu thuẫn.

## 7. Reproducibility

- Không dùng model/LLM, không random; kết quả chỉ phụ thuộc MCP evidence.
- Python 3.11, dependencies theo `pyproject.toml`; chạy tuần tự từng case, trong case chạy song song 3 specialist.
- Lệnh: `day09 run && day09 validate && day09 package --output dist/submission.zip`.
- Kiểm thử luật: `pytest tests/test_rules.py`.
