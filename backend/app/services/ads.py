"""应用层输出业务规则：状态流转、字段校验与筛选口径都收在这里。

应用层输出页必须同步汇总层当前可消费的聚合版本：列表/详情都带上
「可消费版本 / 可消费窗口 / 消费状态」，待复核状态随汇总层一起透出。
"""
from __future__ import annotations

from typing import Any

from app.services import versioning as v
from app.store import store

MODULE = "ads"
REQUIRED_FIELDS = ["流水编号", "输入表名", "输出表名"]
STATUS_ORDER = ["草稿", "运行中", "已暂停", "已完成"]
ACTION_RULES = {"启动输出": "运行中", "暂停输出": "已暂停", "归档流水": "已完成"}
NEGATIVE_ACTIONS = ["暂停输出"]

ADS_DEFAULTS = {
    "汇总表名": None,
    "消费版本": None,
    "消费窗口": "",
    "消费状态": "尚无可消费版本",
}


def summary_table(row: dict[str, Any]) -> str:
    return str(row.get("汇总表名") or row.get("输入表名") or "").strip()


def _sync(row: dict[str, Any]) -> dict[str, Any]:
    """把汇总层当前可消费版本同步到应用层输出视图。"""
    table = summary_table(row)
    state = v.consumable(table)
    pending = v.pending_reviews(table)
    view = dict(row)
    view["可消费版本"] = state["可消费版本"]
    view["可消费窗口"] = state["可消费窗口"]
    view["消费状态"] = state["可消费状态"]
    view["沿用上一版本的窗口"] = state.get("沿用上一版本的窗口", "")
    view["待复核数量"] = len(pending)
    view["待复核分区"] = ",".join(str(item.get("分区")) for item in pending)
    if pending:
        # 待复核结果不能被下游继续消费；未受影响窗口仍按当前可消费版本提供。
        view["消费状态"] = "待复核，暂不消费"
        view["pending"] = True
        view["abnormal"] = True
    elif state["可消费状态"] == "沿用上一版本":
        view["pending"] = True
    return view


class AdsService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [_sync(row) for row in store.rows(MODULE)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("流水编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        view = _sync(row)
        table = summary_table(row)
        view["可消费分区版本"] = [
            {**item, "状态": v.show_version_state(str(item.get("状态")))}
            for item in v.latest_partition_versions(table)
        ]
        view["待复核记录"] = v.pending_reviews(table)
        view["汇总层批次"] = v.batches_for_table(table)
        return view

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry.update(ADS_DEFAULTS)
        for field, value in values.items():
            if field in entry and value not in (None, ""):
                entry[field] = value
        if not entry.get("汇总表名"):
            entry["汇总表名"] = entry.get("输入表名")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"输出流水 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于应用层输出可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"输出流水已{action}"
