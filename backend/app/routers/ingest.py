"""结果入库链路：来源送达、入库确认与批次追踪复查。

与通用资源路由并列，承载「结果存储 × 批次追踪」的真实业务流程：
送达幂等落库、确认断点续做、追踪入口核对实际落库统计。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services.batch_tracker import BatchTrackerService
from app.services.result_store import CONFIRM_DONE, ResultStoreService

router = APIRouter(prefix="/api/ingest", tags=["结果入库"])

_store = ResultStoreService()
_tracker = BatchTrackerService()


def _receipt_view(receipt: dict[str, Any], *, include_rows: bool = False) -> dict[str, Any]:
    view = {
        "receipt_id": receipt["id"],
        "批次标识": receipt["批次标识"],
        "来源标识": receipt["来源标识"],
        "提交标识": receipt["提交标识"],
        "调度任务": receipt.get("调度任务"),
        "存储批次": receipt["存储批次"],
        "原始行数": receipt["原始行数"],
        "有效行数": receipt["有效行数"],
        "内容摘要": receipt["内容摘要"],
        "确认状态": receipt["确认状态"],
        "送达次数": receipt["送达次数"],
        "首次接收时间": receipt["首次接收时间"],
        "最近接收时间": receipt["最近接收时间"],
        "确认时间": receipt.get("确认时间"),
        "确认结果": receipt.get("确认结果"),
        "送达记录": receipt.get("送达记录", []),
    }
    if include_rows:
        view["落库结果行"] = receipt.get("stored_rows", [])
    return view


@router.post("/deliveries")
def receive_delivery(payload: dict[str, Any]) -> dict[str, Any]:
    """接收来源送达：幂等落库并登记入库确认明细。"""
    batch_id = str(payload.get("batch_id") or "").strip()
    source_key = str(payload.get("source_key") or "").strip()
    commit_id = str(payload.get("commit_id") or "").strip()
    rows = payload.get("rows")
    if not batch_id or not source_key or not commit_id:
        raise HTTPException(status_code=400, detail="batch_id、source_key、commit_id 均不能为空")
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=400, detail="rows 必须是非空结果行数组")

    outcome = _store.receive_delivery(
        batch_id=batch_id,
        source_key=source_key,
        commit_id=commit_id,
        rows=rows,
        valid_count=payload.get("valid_count"),
        schedule_id=payload.get("schedule_id"),
    )
    tracking = _tracker.find_tracking(batch_id, source_key)
    return {
        "ok": True,
        "duplicate": outcome["duplicate"],
        "message": outcome["message"],
        "receipt": _receipt_view(outcome["receipt"]),
        "store_batch": outcome["store_batch"],
        "tracking": tracking[0] if tracking else None,
    }


@router.get("/receipts")
def list_receipts(
    batch_id: str | None = Query(default=None),
    source_key: str | None = Query(default=None),
    commit_id: str | None = Query(default=None),
    confirm_status: str | None = Query(default=None),
) -> dict[str, Any]:
    """按批次/来源/提交/确认状态查询入库确认明细。"""
    rows = _store.list_receipts(
        batch_id=batch_id, source_key=source_key,
        commit_id=commit_id, confirm_status=confirm_status,
    )
    return {"items": [_receipt_view(row) for row in rows], "total": len(rows)}


@router.get("/receipts/{receipt_id}")
def get_receipt(receipt_id: int) -> dict[str, Any]:
    """单条确认明细，含实际落库的结果行快照（用于和到账证据逐行核对）。"""
    receipt = _store.get_receipt(receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"入库确认明细 {receipt_id} 不存在")
    return _receipt_view(receipt, include_rows=True)


@router.post("/receipts/{receipt_id}/confirm")
def confirm_receipt(receipt_id: int) -> dict[str, Any]:
    """确认入库：对账失败保持待确认可续做；重复确认返回同一份已确认结果。"""
    receipt, message, ok = _store.confirm_receipt(receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail=message)
    tracking = _tracker.find_tracking(receipt["批次标识"], receipt["来源标识"])
    return {
        "ok": ok,
        "message": message,
        "receipt": _receipt_view(receipt, include_rows=True),
        "tracking": tracking[0] if tracking else None,
    }


@router.get("/tracking")
def tracking_overview() -> dict[str, Any]:
    """批次追踪总览：统计实时读取入库确认明细重新计算。"""
    return _tracker.tracking_overview()


@router.get("/tracking/lookup")
def tracking_lookup(
    batch_id: str = Query(...),
    source_key: str | None = Query(default=None),
) -> dict[str, Any]:
    """按批次标签（可再按来源）复查追踪批次，同标签不同来源各自独立。"""
    items = _tracker.find_tracking(batch_id, source_key)
    return {"batch_id": batch_id, "source_key": source_key, "items": items, "total": len(items)}


@router.get("/tracking/{track_id}")
def tracking_detail(track_id: int) -> dict[str, Any]:
    """单个追踪批次详情，含每条确认明细的实际落库与确认情况。"""
    detail = _tracker.tracking_detail(track_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"追踪批次 {track_id} 不存在")
    return detail
