"""汇总层聚合业务规则：状态流转、字段校验与筛选口径都收在这里。

聚合重算的窗口判断、幂等提交、版本核对与迟到数据待复核都走 services.versioning，
路由层只做参数转换。
"""
from __future__ import annotations

from typing import Any

from app.services import versioning as v
from app.store import store

MODULE = "dws"
REQUIRED_FIELDS = ["流水编号", "输入表名", "输出表名"]
STATUS_ORDER = ["草稿", "运行中", "已暂停", "已完成"]
ACTION_RULES = {"启动聚合": "运行中", "暂停聚合": "已暂停", "归档流水": "已完成"}
NEGATIVE_ACTIONS = ["暂停聚合"]

ENTRY_DEFAULTS = {
    "事实编号": None,
    "聚合字段": "",
    "汇总粒度": "按日",
    "时间窗口": "",
    "预计算开关": "关闭",
    "执行批次": "",
    "待重算": False,
    "重算状态": "已生效",
    "口径版本": None,
    "可消费版本": None,
}


def _views() -> list[dict[str, Any]]:
    """列表/详情统一带上事实口径与可消费版本，前端不用二次拼接。"""
    rows: list[dict[str, Any]] = []
    for entry in store.rows(MODULE):
        state = v.entry_state(entry, v.linked_fact(entry))
        merged = dict(entry)
        merged.update(state)
        merged["status"] = entry.get("status")
        merged["id"] = entry.get("id")
        merged["流水编号"] = entry.get("流水编号")
        merged["输入表名"] = entry.get("输入表名")
        merged["输出表名"] = entry.get("输出表名")
        merged["聚合字段"] = entry.get("聚合字段")
        merged["汇总粒度"] = entry.get("汇总粒度")
        merged["时间窗口"] = entry.get("时间窗口")
        merged["预计算开关"] = entry.get("预计算开关")
        merged["执行批次"] = entry.get("执行批次")
        v.sync_entry_flags(entry, v.linked_fact(entry))
        merged["pending"] = entry.get("pending")
        merged["abnormal"] = entry.get("abnormal")
        rows.append(merged)
    return rows


class DwsService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = _views()
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("流水编号", ""))
                or keyword in str(row.get("输出表名", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        view = dict(entry)
        view.update(v.entry_state(entry, v.linked_fact(entry)))
        view["待复核记录"] = v.pending_reviews(str(entry.get("输出表名")))
        view["批次历史"] = v.batches_for_table(str(entry.get("输出表名")))
        return view

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry.update(ENTRY_DEFAULTS)
        for field, value in values.items():
            if field in entry and value not in (None, ""):
                entry[field] = value
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"聚合流水 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于汇总层聚合可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"聚合流水已{action}"

    # ------------------------------------------------------------ 重算

    def plan(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"聚合流水 {entry_id} 不存在或已归档"
        fact = v.linked_fact(entry)
        plan = v.plan_recalculation(entry, fact)
        plan.update({
            "流水编号": entry.get("流水编号"),
            "输出表名": entry.get("输出表名"),
            "汇总粒度": plan["汇总粒度"],
            "时间窗口": plan["时间窗口"],
            "事实编号": (fact or {}).get("事实编号"),
            "事实口径版本": plan["事实口径版本"],
            "口径生效时间": plan["口径生效时间"],
            "受影响窗口": plan["受影响窗口"],
            "待处理分区": plan["待处理分区"],
            "已生效分区": plan["已生效分区"],
            "受影响": plan["受影响"],
            "判断依据": plan["判断依据"],
        })
        return plan, ""

    def recalculate(self, entry_id: int, *, note: str = "") -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"聚合流水 {entry_id} 不存在或已归档"
        result = v.recalculate(entry, v.linked_fact(entry), note=note)
        entry_view = dict(entry)
        entry_view.update(v.entry_state(entry, v.linked_fact(entry)))
        return {"result": result, "entry": entry_view}, result["message"]

    def verify_batch(self, entry_id: int, batch_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"聚合流水 {entry_id} 不存在或已归档"
        batch = v.find_batch(batch_id)
        if batch is None:
            return None, f"重算批次 {batch_id} 不存在"
        if batch.get("聚合流水") != entry.get("流水编号"):
            return None, f"重算批次 {batch_id} 不属于聚合流水 {entry.get('流水编号')}"
        result = v.verify_batch(entry, v.linked_fact(entry), batch)
        entry_view = dict(entry)
        entry_view.update(v.entry_state(entry, v.linked_fact(entry)))
        return {"result": result, "entry": entry_view}, result["message"]

    def late_partition(
        self,
        entry_id: int,
        *,
        partition: str,
        updated_at: str,
        note: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"聚合流水 {entry_id} 不存在或已归档"
        if not str(partition or "").strip():
            return None, "请提供迟到分区"
        result = v.register_late_partition(entry, v.linked_fact(entry), partition=partition, updated_at=updated_at, note=note)
        entry_view = dict(entry)
        entry_view.update(v.entry_state(entry, v.linked_fact(entry)))
        return {"result": result, "entry": entry_view}, result["message"]
