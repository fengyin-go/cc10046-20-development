"""结果入库链路端到端测试。

覆盖题面场景：
- 入库确认明细关联来源标识与提交标识，存储每次实际收到的结果；
- 确认动作失败可断点续做，同一结果重复送达不会重复入库；
- 批次追踪读取确认明细，刷新/重进可核对实际落库统计与首次收到的版本；
- 重送保留原结果；同标签两个来源各自独立、到账证据不串记；
- 第 3 次送达原提交后，从追踪入口复查两批，两种处理都返回同一已确认结果。
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from app.main import app
from app.services.result_store import (
    CONFIRM_DONE,
    CONFIRM_PENDING,
    RECEIPT_MODULE,
    ResultStoreService,
    canonical_digest,
)
from app.store import store

client = TestClient(app)


def _rows(prefix: str, count: int):
    return [
        {"row_no": index, "batch_label": "LBL-E2E", "payload": f"{prefix}-row-{index}"}
        for index in range(1, count + 1)
    ]


def _deliver(label: str, source: str, commit: str, rows, **extra):
    payload = {
        "batch_id": label,
        "source_key": source,
        "commit_id": commit,
        "rows": rows,
    }
    payload.update(extra)
    return client.post("/api/ingest/deliveries", json=payload)


def test_delivery_persists_receipt_with_source_and_commit_credentials():
    rows = _rows("甲", 9)
    resp = _deliver("LBL-CRED", "src-a", "cm-cred-1", rows)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["duplicate"] is False
    receipt = body["receipt"]
    # 持久入库凭据：来源 + 提交标识 + 每次实际收到的结果摘要
    assert receipt["来源标识"] == "src-a"
    assert receipt["提交标识"] == "cm-cred-1"
    assert receipt["确认状态"] == CONFIRM_PENDING
    assert receipt["原始行数"] == 9
    assert receipt["内容摘要"] == canonical_digest(rows)
    assert receipt["送达次数"] == 1
    assert len(receipt["送达记录"]) == 1
    # 明细与存储批次都已持久化，且对得上
    stored = ResultStoreService().get_receipt(receipt["receipt_id"])
    assert stored is not None
    assert stored["stored_rows"] == rows
    assert stored["存储批次"] == body["store_batch"]["id"]
    assert body["store_batch"]["内容摘要"] == receipt["内容摘要"]


def test_redelivery_keeps_original_result_and_never_duplicates_storage():
    rows = _rows("甲", 9)
    first = _deliver("LBL-RETRY", "src-a", "cm-retry-1", rows).json()
    receipt_id = first["receipt"]["receipt_id"]
    batch_id = first["store_batch"]["id"]
    store_count_before = len(store.rows("result_store"))

    # 第 2 次送达同一提交：重送，不新增落库记录
    second = _deliver("LBL-RETRY", "src-a", "cm-retry-1", rows).json()
    assert second["duplicate"] is True
    assert second["receipt"]["receipt_id"] == receipt_id
    assert second["store_batch"]["id"] == batch_id
    assert len(store.rows("result_store")) == store_count_before
    # 首次收到的原始结果原样保留，仅累计送达次数
    assert second["receipt"]["送达次数"] == 2
    stored = ResultStoreService().get_receipt(receipt_id)
    assert stored["stored_rows"] == rows
    assert len(stored["送达记录"]) == 2
    assert stored["内容摘要"] == canonical_digest(rows)


def test_same_label_two_sources_keep_independent_records():
    rows_a = _rows("甲", 9)
    rows_b = _rows("乙", 7)
    a = _deliver("LBL-SAME", "src-a", "cm-same-a", rows_a).json()
    b = _deliver("LBL-SAME", "src-b", "cm-same-b", rows_b).json()

    # 标签相同，但来源不同 → 明细与存储批次各自独立
    assert a["receipt"]["receipt_id"] != b["receipt"]["receipt_id"]
    assert a["store_batch"]["id"] != b["store_batch"]["id"]
    assert a["receipt"]["内容摘要"] != b["receipt"]["内容摘要"]
    assert a["store_batch"]["存储路径"] != b["store_batch"]["存储路径"]

    lookup = client.get("/api/ingest/tracking/lookup", params={"batch_id": "LBL-SAME"}).json()
    assert lookup["total"] == 2
    by_source = {item["来源标识"]: item for item in lookup["items"]}
    # 甲的 9 行到账证据不能记给乙（乙只有 7 行）
    assert by_source["src-a"]["采集总数"] == "9"
    assert by_source["src-b"]["采集总数"] == "7"
    assert by_source["src-a"]["首次提交"]["提交标识"] == "cm-same-a"
    assert by_source["src-b"]["首次提交"]["提交标识"] == "cm-same-b"


def test_confirmation_failure_resumable_and_does_not_confirm_on_mismatch():
    rows = _rows("甲", 9)
    body = _deliver("LBL-FAIL", "src-a", "cm-fail-1", rows).json()
    receipt_id = body["receipt"]["receipt_id"]
    svc = ResultStoreService()
    receipt = svc.get_receipt(receipt_id)

    # 模拟确认动作中断前落库证据被破坏：对账必须失败，明细保持待确认
    receipt["stored_rows"][0] = {"row_no": 1, "payload": "tampered"}
    _, message, ok = svc.confirm_receipt(receipt_id)
    assert ok is False
    assert "对账失败" in message
    receipt = svc.get_receipt(receipt_id)
    assert receipt["确认状态"] == CONFIRM_PENDING
    assert receipt["abnormal"] is True

    # 断点续做：恢复真实结果后重新确认成功
    receipt["stored_rows"] = [dict(r) for r in rows]
    receipt2, message2, ok2 = svc.confirm_receipt(receipt_id)
    assert ok2 is True
    assert receipt2["确认状态"] == CONFIRM_DONE
    assert receipt2["确认结果"]["matched"] is True

    # 已确认后再次确认，返回同一份已确认结果（确认时间不被覆盖）
    confirmed_at = receipt2["确认时间"]
    again, message3, ok3 = svc.confirm_receipt(receipt_id)
    assert ok3 is True
    assert again["确认时间"] == confirmed_at
    assert "已确认" in message3


def test_full_duty_scenario_nine_and_seven_rows_with_third_redelivery():
    """题面主场景：挂起甲的确认 → 刷新 → 第 3 次送达甲 → 追踪入口复查两批。"""
    rows_a = _rows("甲", 9)
    rows_b = _rows("乙", 7)
    first_a = _deliver("LBL-DUTY", "src-a", "cm-duty-a", rows_a).json()
    first_b = _deliver("LBL-DUTY", "src-b", "cm-duty-b", rows_b).json()
    id_a = first_a["receipt"]["receipt_id"]
    id_b = first_b["receipt"]["receipt_id"]

    # 甲的确认被挂起：只确认乙，甲保持「结果已收但批次待确认」
    ok_b = client.post(f"/api/ingest/receipts/{id_b}/confirm").json()
    assert ok_b["ok"] is True
    assert ok_b["receipt"]["确认状态"] == CONFIRM_DONE

    # 值班员打开批次追踪：能看到甲已有 9 行真实结果、仍待确认
    overview = client.get("/api/ingest/tracking").json()
    duty = {b["来源标识"]: b for b in overview["items"] if b["批次标识"] == "LBL-DUTY"}
    assert duty["src-a"]["采集总数"] == "9"
    assert duty["src-a"]["入库状态"] == CONFIRM_PENDING
    assert duty["src-a"]["待确认提交"] == 1
    assert duty["src-a"]["pending_receipt_ids"] == [id_a]
    assert duty["src-b"]["采集总数"] == "7"
    assert duty["src-b"]["入库状态"] == CONFIRM_DONE

    # 刷新页面（重新进入）：统计仍可复核，首次收到的版本查得到
    refreshed = client.get("/api/ingest/tracking/lookup",
                           params={"batch_id": "LBL-DUTY", "source_key": "src-a"}).json()
    track_a = refreshed["items"][0]
    assert track_a["采集总数"] == "9"
    assert track_a["首次提交"]["提交标识"] == "cm-duty-a"
    assert track_a["首次提交"]["原始行数"] == 9

    # 第 3 次送达甲的原提交（此前已送 2 次的累计在本场景为首次后的又一重送）
    redeliver = _deliver("LBL-DUTY", "src-a", "cm-duty-a", rows_a).json()
    assert redeliver["duplicate"] is True
    assert redeliver["receipt"]["receipt_id"] == id_a
    assert redeliver["receipt"]["送达次数"] == 2
    # 原结果保留
    detail = client.get(f"/api/ingest/receipts/{id_a}").json()
    assert detail["落库结果行"] == rows_a

    # 路径一：从追踪入口补全确认
    track_a = client.get("/api/ingest/tracking/lookup",
                         params={"batch_id": "LBL-DUTY", "source_key": "src-a"}).json()["items"][0]
    confirm_via_tracking = client.post(
        f"/api/ingest/receipts/{track_a['pending_receipt_ids'][0]}/confirm").json()
    assert confirm_via_tracking["ok"] is True
    confirmed_time = confirm_via_tracking["receipt"]["确认时间"]

    # 路径二：来源再次送达后确认，返回同一个已确认结果
    third = _deliver("LBL-DUTY", "src-a", "cm-duty-a", rows_a).json()
    assert third["receipt"]["receipt_id"] == id_a
    confirm_again = client.post(f"/api/ingest/receipts/{id_a}/confirm").json()
    assert confirm_again["ok"] is True
    assert confirm_again["receipt"]["确认时间"] == confirmed_time
    assert confirm_again["receipt"]["确认状态"] == CONFIRM_DONE

    # 两批最终都已确认，且行数仍分别是 9 与 7
    final = client.get("/api/ingest/tracking/lookup",
                       params={"batch_id": "LBL-DUTY"}).json()
    final_by_source = {item["来源标识"]: item for item in final["items"]}
    assert final_by_source["src-a"]["入库状态"] == CONFIRM_DONE
    assert final_by_source["src-a"]["采集总数"] == "9"
    assert final_by_source["src-b"]["入库状态"] == CONFIRM_DONE
    assert final_by_source["src-b"]["采集总数"] == "7"
    # 全流程没有产生重复入库：两条明细对应两个存储批次
    receipts = ResultStoreService().list_receipts(batch_id="LBL-DUTY")
    assert len(receipts) == 2
    assert {r["存储批次"] for r in receipts} == {
        first_a["store_batch"]["id"], first_b["store_batch"]["id"]}


def test_confirm_unknown_receipt_returns_404_and_validation_errors():
    resp = client.post("/api/ingest/receipts/999999/confirm")
    assert resp.status_code == 404

    bad = client.post("/api/ingest/deliveries", json={
        "batch_id": "LBL-BAD", "source_key": "src-a", "commit_id": "c1", "rows": [],
    })
    assert bad.status_code == 400

    missing = client.post("/api/ingest/deliveries", json={"rows": [{"a": 1}]})
    assert missing.status_code == 400


def test_receipt_list_filters_and_legacy_chain_still_work():
    rows = _rows("甲", 3)
    _deliver("LBL-FILTER", "src-a", "cm-f-1", rows)
    by_batch = client.get("/api/ingest/receipts", params={"batch_id": "LBL-FILTER"}).json()
    assert by_batch["total"] == 1
    by_status = client.get("/api/ingest/receipts", params={"confirm_status": CONFIRM_PENDING}).json()
    assert all(item["确认状态"] == CONFIRM_PENDING for item in by_status["items"])

    # 旧链路 domain_store_result 仍可用，并自动带上来源/提交凭据
    legacy = ResultStoreService().domain_store_result("LBL-LEGACY", "legacy-cfg", 12, 10)
    assert legacy["批次标识"] == "LBL-LEGACY"
    assert legacy["原始行数"] == "12"
    receipt = ResultStoreService().find_receipt("LBL-LEGACY", "legacy-cfg", "legacy:LBL-LEGACY")
    assert receipt is not None and len(receipt["stored_rows"]) == 12
