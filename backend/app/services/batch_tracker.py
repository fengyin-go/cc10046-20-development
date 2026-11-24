"""批次追踪领域服务。

批次追踪的真实进度以结果存储侧的入库确认明细（``result_receipt``）为准：
每次结果送达都会在这里维护一个 (批次标识, 来源标识) 维度的追踪批次，
页面刷新或重新进入时重新读取确认明细，即可核对批次实际落库统计、
首次收到的提交版本以及确认中断情况，确认动作可从追踪入口断点续做。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.result_store import CONFIRM_DONE, CONFIRM_PENDING, RECEIPT_MODULE


def _safe_int(value: Any, default: int = 0) -> int:
    """兼容种子数据里的非数字占位文本。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class BatchTrackerService(BaseModuleService):
    MODULE = "batch_tracker"
    ENTITY = "采集批次"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["批次标识", "调度任务", "开始时间", "结束时间", "采集总数", "失败行数", "水位标记", "批次状态"]
    REQUIRED_FIELDS = ["批次标识", "调度任务", "开始时间"]
    STATUS_ORDER = ["执行中", "部分成功", "全部成功", "采集失败"]
    ACTION_RULES = {"重采失败行": "部分成功", "标记完成": "全部成功"}
    NEGATIVE_ACTIONS = []

    def domain_track_batch(self, batch_id: str, schedule_id: str) -> dict:
        """追踪采集批次执行状态（旧接口：没有来源维度时按默认来源登记）。"""
        return self.upsert_delivery_batch(
            batch_id=batch_id,
            source_key="default",
            schedule_id=schedule_id,
            receipt=None,
        )

    # ------------------------------------------------------------------
    # 追踪批次维护：以确认明细为事实来源，幂等 upsert。
    # ------------------------------------------------------------------
    def upsert_delivery_batch(
        self,
        *,
        batch_id: str,
        source_key: str,
        schedule_id: str,
        receipt: dict[str, Any] | None,
    ) -> dict[str, Any]:
        from app.store import store

        rows = store.rows(self.MODULE)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = self._find_track(batch_id, source_key)

        if entry is None:
            entry = {
                "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
                "批次标识": batch_id,
                "来源标识": source_key,
                "调度任务": schedule_id,
                "开始时间": now,
                "结束时间": None,
                "采集总数": "0",
                "失败行数": "0",
                "水位标记": now,
                "批次状态": "执行中",
                "入库状态": CONFIRM_PENDING,
                "首次提交": None,
                "已确认提交": 0,
                "待确认提交": 0,
                "送达次数": 0,
                "确认明细": [],
                "status": "执行中",
                "pending": True,
                "abnormal": False,
            }
            rows.append(entry)

        if receipt is not None:
            self._apply_receipt(entry, receipt)
        return entry

    def _apply_receipt(self, entry: dict[str, Any], receipt: dict[str, Any]) -> None:
        """用一条确认明细刷新追踪批次的实际落库统计。"""
        related = [rid for rid in entry.get("确认明细", []) if rid != receipt["id"]]
        related.append(receipt["id"])
        entry["确认明细"] = related
        entry["来源标识"] = receipt.get("来源标识", entry.get("来源标识"))
        entry["调度任务"] = receipt.get("调度任务") or entry.get("调度任务")

        receipts = self._receipts_for(entry)
        total_rows = sum(_safe_int(r.get("原始行数")) for r in receipts)
        valid_rows = sum(_safe_int(r.get("有效行数")) for r in receipts)
        confirmed = [r for r in receipts if r.get("确认状态") == CONFIRM_DONE]
        pending = [r for r in receipts if r.get("确认状态") == CONFIRM_PENDING]
        deliveries = sum(int(r.get("送达次数", 1)) for r in receipts)

        entry["采集总数"] = str(total_rows)
        entry["失败行数"] = str(max(total_rows - valid_rows, 0))
        entry["已确认提交"] = len(confirmed)
        entry["待确认提交"] = len(pending)
        entry["送达次数"] = deliveries
        entry["最近接收时间"] = max(
            (r.get("最近接收时间", "") for r in receipts), default=entry["开始时间"]
        )
        # 首次收到的提交版本（同标签重送不会覆盖）。
        first = min(receipts, key=lambda r: r.get("首次接收时间", ""), default=None)
        entry["首次提交"] = (
            {"提交标识": first["提交标识"], "首次接收时间": first["首次接收时间"],
             "内容摘要": first["内容摘要"], "原始行数": first["原始行数"]}
            if first else None
        )

        if pending:
            entry["入库状态"] = CONFIRM_PENDING
            entry["批次状态"] = "部分成功" if confirmed else "执行中"
            entry["status"] = entry["批次状态"]
            entry["pending"] = True
            entry["abnormal"] = any(r.get("abnormal") for r in pending)
        else:
            entry["入库状态"] = CONFIRM_DONE
            entry["批次状态"] = "全部成功"
            entry["status"] = "全部成功"
            entry["pending"] = False
            entry["abnormal"] = False
            entry["结束时间"] = max(
                (r.get("确认时间") or r.get("最近接收时间", "") for r in receipts),
                default=entry.get("结束时间"),
            )
        entry["水位标记"] = entry.get("最近接收时间") or entry["水位标记"]

    # ------------------------------------------------------------------
    # 追踪读取：重新读取确认明细，保证刷新/重进统计可复核。
    # ------------------------------------------------------------------
    def tracking_overview(self) -> dict[str, Any]:
        from app.store import store

        batches = []
        for entry in store.rows(self.MODULE):
            batches.append(self.tracking_detail(int(entry["id"])))
        pending_batches = sum(1 for b in batches if b["入库状态"] == CONFIRM_PENDING)
        return {
            "批次总数": len(batches),
            "待确认批次": pending_batches,
            "已确认批次": len(batches) - pending_batches,
            "实际落库行数": sum(_safe_int(b["采集总数"]) for b in batches),
            "items": batches,
        }

    def tracking_detail(self, entry_id: int) -> dict[str, Any] | None:
        from app.store import store

        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        # 重新依据确认明细计算，页面刷新或重新进入看到的都是最新事实。
        for receipt in self._receipts_for(entry):
            self._apply_receipt(entry, receipt)
        return self._public_view(entry)

    def find_tracking(self, batch_id: str, source_key: str | None = None) -> list[dict[str, Any]]:
        from app.store import store

        result = []
        for entry in store.rows(self.MODULE):
            if entry.get("批次标识") != batch_id:
                continue
            if source_key and entry.get("来源标识") != source_key:
                continue
            view = self.tracking_detail(int(entry["id"]))
            if view is not None:
                result.append(view)
        return result

    def _find_track(self, batch_id: str, source_key: str) -> dict[str, Any] | None:
        from app.store import store

        for row in store.rows(self.MODULE):
            if row.get("批次标识") == batch_id and row.get("来源标识") == source_key:
                return row
        return None

    def _receipts_for(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        from app.store import store

        receipts = store.rows(RECEIPT_MODULE)
        ids = set(entry.get("确认明细", []))
        related = [r for r in receipts if r["id"] in ids]
        if not related:
            # 兼容旧数据：没有明细 id 时按批次 + 来源回查。
            related = [
                r for r in receipts
                if r.get("批次标识") == entry.get("批次标识")
                and r.get("来源标识") == entry.get("来源标识", "default")
            ]
        return related

    def _public_view(self, entry: dict[str, Any]) -> dict[str, Any]:
        from app.store import store

        receipts = []
        for receipt in self._receipts_for(entry):
            receipts.append({
                "receipt_id": receipt["id"],
                "提交标识": receipt["提交标识"],
                "来源标识": receipt["来源标识"],
                "原始行数": receipt["原始行数"],
                "有效行数": receipt["有效行数"],
                "内容摘要": receipt["内容摘要"],
                "确认状态": receipt["确认状态"],
                "送达次数": receipt["送达次数"],
                "首次接收时间": receipt["首次接收时间"],
                "最近接收时间": receipt["最近接收时间"],
                "确认时间": receipt.get("确认时间"),
                "存储批次": receipt["存储批次"],
            })
        return {
            "track_id": entry["id"],
            "批次标识": entry["批次标识"],
            "来源标识": entry.get("来源标识", "default"),
            "调度任务": entry.get("调度任务"),
            "批次状态": entry["批次状态"],
            "入库状态": entry.get("入库状态", CONFIRM_PENDING),
            "采集总数": entry["采集总数"],
            "失败行数": entry["失败行数"],
            "已确认提交": entry.get("已确认提交", 0),
            "待确认提交": entry.get("待确认提交", 0),
            "送达次数": entry.get("送达次数", 0),
            "首次提交": entry.get("首次提交"),
            "开始时间": entry["开始时间"],
            "最近接收时间": entry.get("最近接收时间"),
            "结束时间": entry.get("结束时间"),
            "确认明细": receipts,
            # 追踪入口补全确认需要的明细 id。
            "pending_receipt_ids": [r["id"] for r in self._receipts_for(entry)
                                    if r.get("确认状态") == CONFIRM_PENDING],
            "status": entry["status"],
            "pending": entry.get("pending", True),
            "abnormal": entry.get("abnormal", False),
        }
