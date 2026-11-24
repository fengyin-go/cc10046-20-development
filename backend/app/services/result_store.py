"""结果存储领域服务。

在通用存储批次骨架之上，补齐结果送达的持久入库凭据：

- 每次实际收到的结果都会落到 ``result_store`` 存储批次，并写一条
  ``result_receipt`` 入库确认明细，明细关联来源标识与提交标识；
- 以「批次标识 + 来源标识 + 提交标识」为幂等键，同一提交重复送达时
  保留首次落库的原始结果，只累计送达次数，绝不重复入库；
- 确认动作基于落库结果行重算摘要对账，对账不通过或中途失败时明细保持
  「待确认」，值班员可断点续做，重复确认返回同一份已确认结果；
- 两个来源即使批次标签相同，也各自拥有独立的存储批次与确认明细，
  到账证据不会跨站串记。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from app.services.base import BaseModuleService

RECEIPT_MODULE = "result_receipt"
CONFIRM_PENDING = "待确认"
CONFIRM_DONE = "已确认"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def canonical_digest(rows: list[dict[str, Any]]) -> str:
    """对结果行做规范化哈希，作为入库与确认两侧共同的对账凭据。"""
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ResultStoreService(BaseModuleService):
    MODULE = "result_store"
    ENTITY = "存储批次"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["批次标识", "来源配置", "采集时间", "原始行数", "有效行数", "存储路径", "压缩格式", "校验摘要"]
    REQUIRED_FIELDS = ["批次标识", "来源配置", "采集时间"]
    STATUS_ORDER = ["存储中", "已落库", "校验未过", "已归档"]
    ACTION_RULES = {"执行校验": "已落库", "发起归档": "已落库"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 兼容旧链路：按行数登记存储批次，内部走统一的幂等送达入口。
    # ------------------------------------------------------------------
    def domain_store_result(self, batch_id: str, source_config: str,
                            row_count: int, valid_count: int) -> dict:
        """结构化存储采集结果（旧接口，委托给幂等送达流程）。"""
        rows = [
            {"row_no": index + 1, "batch_id": batch_id, "payload": f"row-{index + 1}"}
            for index in range(int(row_count))
        ]
        outcome = self.receive_delivery(
            batch_id=batch_id,
            source_key=source_config,
            commit_id=f"legacy:{batch_id}",
            rows=rows,
            valid_count=int(valid_count),
        )
        return outcome["store_batch"]

    # ------------------------------------------------------------------
    # 结果送达：幂等入库 + 入库确认明细。
    # ------------------------------------------------------------------
    def receive_delivery(
        self,
        *,
        batch_id: str,
        source_key: str,
        commit_id: str,
        rows: list[dict[str, Any]],
        valid_count: int | None = None,
        schedule_id: str | None = None,
    ) -> dict[str, Any]:
        """接收一次来源送达并落库，返回存储批次、确认明细与是否重送。

        同一 (批次标识, 来源标识, 提交标识) 的重复送达不会产生第二条落库
        记录：首次收到的结果行原样保留，仅追加送达凭据并累计送达次数。
        """
        from app.store import store

        batch_id = str(batch_id or "").strip()
        source_key = str(source_key or "").strip()
        commit_id = str(commit_id or "").strip()
        if not batch_id or not source_key or not commit_id:
            raise ValueError("批次标识、来源标识、提交标识均不能为空")
        if not isinstance(rows, list):
            raise ValueError("rows 必须是结果行数组")

        receipts = store.rows(RECEIPT_MODULE)
        existing = self._find_receipt(batch_id, source_key, commit_id)
        now = _now()

        if existing is not None:
            # 重送：保留首次落库的原始结果，只补充本次送达凭据。
            existing["送达次数"] = int(existing.get("送达次数", 1)) + 1
            existing["最近接收时间"] = now
            existing.setdefault("送达记录", []).append({
                "时间": now,
                "提交标识": commit_id,
                "原始行数": len(rows),
                "内容摘要": canonical_digest(rows),
            })
            store_batch = store.find(self.MODULE, int(existing["存储批次"]))
            self._sync_tracker(batch_id, source_key, schedule_id, existing)
            return {
                "duplicate": True,
                "receipt": existing,
                "store_batch": store_batch,
                "message": "提交已入库，本次为重复送达，保留首次落库结果",
            }

        digest = canonical_digest(rows)
        row_count = len(rows)
        if valid_count is None:
            valid_count = row_count
        valid_count = int(valid_count)

        store_rows = store.rows(self.MODULE)
        store_entry = {
            "id": max((int(r.get("id", 0)) for r in store_rows), default=0) + 1,
            "批次标识": batch_id,
            "来源配置": source_key,
            "来源标识": source_key,
            "提交标识": commit_id,
            "采集时间": time.strftime("%Y-%m-%d"),
            "原始行数": str(row_count),
            "有效行数": str(valid_count),
            "存储路径": f"/data/results/{source_key}/{batch_id}/{commit_id}.json.gz",
            "压缩格式": "gzip",
            "校验摘要": digest,
            "内容摘要": digest,
            "送达次数": 1,
            "首次接收时间": now,
            "最近接收时间": now,
            "status": "已落库",
            "pending": False,
            "abnormal": False,
        }
        store_rows.append(store_entry)

        receipt = {
            "id": max((int(r.get("id", 0)) for r in receipts), default=0) + 1,
            "批次标识": batch_id,
            "来源标识": source_key,
            "提交标识": commit_id,
            "调度任务": schedule_id,
            "存储批次": store_entry["id"],
            "原始行数": row_count,
            "有效行数": valid_count,
            "内容摘要": digest,
            "首次接收时间": now,
            "最近接收时间": now,
            "送达次数": 1,
            "确认状态": CONFIRM_PENDING,
            "确认时间": None,
            "确认结果": None,
            "送达记录": [{
                "时间": now,
                "提交标识": commit_id,
                "原始行数": row_count,
                "内容摘要": digest,
            }],
            # 实际收到并落库的结果行快照，确认时与摘要重新对账。
            "stored_rows": [dict(row) for row in rows],
            "status": CONFIRM_PENDING,
            "pending": True,
            "abnormal": False,
        }
        receipts.append(receipt)
        self._sync_tracker(batch_id, source_key, schedule_id, receipt)
        return {
            "duplicate": False,
            "receipt": receipt,
            "store_batch": store_entry,
            "message": "结果已落库，入库确认明细已登记，等待确认",
        }

    # ------------------------------------------------------------------
    # 入库确认：对账通过才置为已确认，失败可断点续做。
    # ------------------------------------------------------------------
    def confirm_receipt(self, receipt_id: int) -> tuple[dict[str, Any] | None, str, bool]:
        """对一条入库确认明细执行确认。

        返回 (明细, 消息, 是否成功)。已确认的明细重复确认时原样返回，
        保证两种处理路径都拿到同一份已确认结果；对账失败保持「待确认」。
        """
        from app.store import store

        receipt = self._get_receipt(receipt_id)
        if receipt is None:
            return None, f"入库确认明细 {receipt_id} 不存在", False

        if receipt.get("确认状态") == CONFIRM_DONE:
            return receipt, "该提交此前已确认，返回首次确认结果，未重复入库", True

        store_entry = store.find(self.MODULE, int(receipt["存储批次"]))
        problems: list[str] = []
        if store_entry is None:
            problems.append(f"关联存储批次 {receipt['存储批次']} 不存在")
        else:
            if int(store_entry.get("原始行数", -1)) != int(receipt["原始行数"]):
                problems.append("存储批次原始行数与确认明细不一致")
            if store_entry.get("内容摘要") != receipt["内容摘要"]:
                problems.append("存储批次内容摘要与确认明细不一致")

        recomputed = canonical_digest(receipt.get("stored_rows", []))
        if recomputed != receipt["内容摘要"]:
            problems.append("落库结果行重算摘要与首次接收摘要不一致")

        if problems:
            receipt["abnormal"] = True
            self._sync_tracker(
                receipt["批次标识"], receipt["来源标识"],
                receipt.get("调度任务"), receipt,
            )
            return receipt, "入库确认对账失败：" + "；".join(problems), False

        now = _now()
        receipt["确认状态"] = CONFIRM_DONE
        receipt["status"] = CONFIRM_DONE
        receipt["pending"] = False
        receipt["abnormal"] = False
        receipt["确认时间"] = now
        receipt["确认结果"] = {
            "matched": True,
            "确认时间": now,
            "核对行数": receipt["原始行数"],
            "内容摘要": receipt["内容摘要"],
            "存储批次": receipt["存储批次"],
        }
        self._sync_tracker(
            receipt["批次标识"], receipt["来源标识"],
            receipt.get("调度任务"), receipt,
        )
        return receipt, "入库确认对账通过，明细已补全为已确认", True

    # ------------------------------------------------------------------
    # 查询辅助。
    # ------------------------------------------------------------------
    def list_receipts(
        self,
        *,
        batch_id: str | None = None,
        source_key: str | None = None,
        commit_id: str | None = None,
        confirm_status: str | None = None,
    ) -> list[dict[str, Any]]:
        from app.store import store

        rows = list(store.rows(RECEIPT_MODULE))
        if batch_id:
            rows = [r for r in rows if r.get("批次标识") == batch_id]
        if source_key:
            rows = [r for r in rows if r.get("来源标识") == source_key]
        if commit_id:
            rows = [r for r in rows if r.get("提交标识") == commit_id]
        if confirm_status:
            rows = [r for r in rows if r.get("确认状态") == confirm_status]
        return rows

    def get_receipt(self, receipt_id: int) -> dict[str, Any] | None:
        return self._get_receipt(receipt_id)

    def find_receipt(self, batch_id: str, source_key: str, commit_id: str) -> dict[str, Any] | None:
        return self._find_receipt(batch_id, source_key, commit_id)

    def _get_receipt(self, receipt_id: int) -> dict[str, Any] | None:
        from app.store import store

        for row in store.rows(RECEIPT_MODULE):
            if int(row.get("id", 0)) == int(receipt_id):
                return row
        return None

    def _find_receipt(self, batch_id: str, source_key: str,
                      commit_id: str) -> dict[str, Any] | None:
        from app.store import store

        for row in store.rows(RECEIPT_MODULE):
            if (row.get("批次标识") == batch_id
                    and row.get("来源标识") == source_key
                    and row.get("提交标识") == commit_id):
                return row
        return None

    def _sync_tracker(self, batch_id: str, source_key: str,
                      schedule_id: str | None, receipt: dict[str, Any]) -> None:
        """把确认明细的落库事实同步给批次追踪，刷新/重进均可复核。"""
        from app.services.batch_tracker import BatchTrackerService

        tracker = BatchTrackerService()
        tracker.upsert_delivery_batch(
            batch_id=batch_id,
            source_key=source_key,
            schedule_id=schedule_id or receipt.get("调度任务") or source_key,
            receipt=receipt,
        )
