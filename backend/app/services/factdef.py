"""事实表管理业务规则：状态流转、字段校验与筛选口径都收在这里。

事实定义发布后修改度量字段会登记一版新的度量口径，并顺着取数链路把受影响窗口
同步给汇总层聚合流水；这里的动作只做口径版本登记，不直接重算。
"""
from __future__ import annotations

from typing import Any

from app.services import versioning as v
from app.store import store

MODULE = "factdef"
REQUIRED_FIELDS = ["事实编号", "事实名称", "源表名称"]
STATUS_ORDER = ["草稿", "待审批", "已生效", "已废弃"]
ACTION_RULES = {"提交审批": "待审批", "发布事实": "已生效", "废弃事实": "已废弃"}
NEGATIVE_ACTIONS = ["废弃事实"]

FACT_DEFAULTS = {
    "粒度说明": "按日",
    "关联维度": "",
    "度量字段": "",
    "分区字段": "",
    "最近刷新": "",
    "口径版本": None,
    "口径生效时间": None,
    "待重算": False,
}


def _views() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fact in store.rows(MODULE):
        view = dict(fact)
        facts_entries = v.downstream_entries(fact)
        plans = [v.plan_recalculation(entry, fact) for entry in facts_entries]
        pending = [plan for plan in plans if plan["受影响"] and plan["待处理分区"]]
        # 待重算只由下游实际待处理分区决定，重算完成并核对后自动消失。
        view["待重算"] = bool(pending)
        view["pending"] = bool(pending) or bool(view.get("abnormal"))
        view["受影响窗口"] = ",".join(
            window for plan in plans if plan["受影响"] for window in plan["受影响窗口"]
        )
        view["下游聚合流水"] = ",".join(str(entry.get("流水编号")) for entry in facts_entries)
        view["下游重算状态"] = ",".join(
            str(v.entry_state(entry, fact)["重算状态"]) for entry in facts_entries
        ) or "无下游聚合流水"
        view["口径版本"] = fact.get("口径版本")
        view["口径生效时间"] = fact.get("口径生效时间")
        rows.append(view)
    return rows


class FactdefService:
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
            rows = [row for row in rows if keyword in str(row.get("事实编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        fact = store.find(MODULE, entry_id)
        if fact is None:
            return None
        view = dict(fact)
        view["口径版本历史"] = v.metric_versions(str(fact.get("事实编号") or ""))
        view["下游聚合流水"] = [
            {
                "流水编号": entry.get("流水编号"),
                "输出表名": entry.get("输出表名"),
                "汇总粒度": v.summary_grain(entry),
                "时间窗口": v.plan_recalculation(entry, fact)["时间窗口"],
                "受影响窗口": v.plan_recalculation(entry, fact)["受影响窗口"],
                "待处理分区": v.plan_recalculation(entry, fact)["待处理分区"],
                "重算状态": v.entry_state(entry, fact)["重算状态"],
                "可消费版本": v.entry_state(entry, fact)["可消费版本"],
            }
            for entry in v.downstream_entries(fact)
        ]
        return view

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry.update(FACT_DEFAULTS)
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
            return None, f"事实定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于事实表管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"事实定义已{action}"

    def change_metrics(
        self,
        entry_id: int,
        *,
        metrics: Any,
        partition_field: str | None = None,
        effective_at: str | None = None,
        note: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """发布后的度量口径变更：登记新口径版本，并把受影响窗口同步到下游聚合流水。"""
        fact = store.find(MODULE, entry_id)
        if fact is None:
            return None, f"事实定义 {entry_id} 不存在或已归档"
        try:
            record = v.register_metric_change(
                fact,
                metrics=metrics,
                partition_field=partition_field,
                effective_at=effective_at,
                note=note,
            )
        except ValueError as error:
            return None, str(error)
        entries = v.mark_downstream(fact)
        view = dict(fact)
        view["口径变更"] = record
        view["下游聚合流水"] = [
            {
                "流水编号": entry.get("流水编号"),
                "输出表名": entry.get("输出表名"),
                "汇总粒度": v.summary_grain(entry),
                "时间窗口": v.plan_recalculation(entry, fact)["时间窗口"],
                "受影响窗口": v.plan_recalculation(entry, fact)["受影响窗口"],
                "待处理分区": v.plan_recalculation(entry, fact)["待处理分区"],
                "重算状态": v.entry_state(entry, fact)["重算状态"],
            }
            for entry in entries
        ]
        if record.get("未变化"):
            message = f"度量口径与当前版本 {record['口径版本']} 一致，未登记新版本，下游无需重算"
        else:
            message = (
                f"事实口径已更新为 {record['口径版本']}（生效 {record['生效时间']}），"
                f"受影响聚合流水 {len(entries)} 条已标记待重算"
            )
        return view, message
