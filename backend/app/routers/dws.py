"""汇总层聚合接口：维护聚合流水，覆盖重算计划、提交重算、核对发布、迟到补算与动作流转。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dws import DwsService
from app.services import versioning as v

router = APIRouter(prefix="/api/dws", tags=["汇总层聚合"])

service = DwsService()

LIST_FIELDS = ["流水编号", "输入表名", "输出表名", "聚合字段", "汇总粒度", "时间窗口", "预计算开关", "执行批次"]
STATUSES = ["草稿", "运行中", "已暂停", "已完成"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按流水编号或输出表名检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已暂停、已完成"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按流水编号与状态过滤汇总层聚合列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出汇总层聚合清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dws", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条聚合流水明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"聚合流水 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条聚合流水，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="聚合流水已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条聚合流水执行启动聚合、暂停聚合、归档流水；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/recalc-plan", response_model=dict)
def recalc_plan(entry_id: int) -> dict:
    """按事实粒度、度量和分区字段判断受影响窗口，再用汇总粒度与时间窗口界定重算范围。"""
    plan, message = service.plan(entry_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=message)
    return plan


@router.post("/{entry_id}/recalculate", response_model=ActionResult)
def recalculate(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交重算：同一时间窗口与口径版本重复提交复用原批次，只处理仍待处理的分区。"""
    note = str(payload.values.get("备注") or payload.remark or "").strip()
    result, message = service.recalculate(entry_id, note=note)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=bool(result["result"]["ok"]), message=message, entry=result["entry"])


@router.post("/{entry_id}/batches/{batch_id}/verify", response_model=ActionResult)
def verify_batch(entry_id: int, batch_id: int) -> ActionResult:
    """一致性核对：通过后新版本才切换为当前可消费版本，旧版本保留为上一版本。"""
    result, message = service.verify_batch(entry_id, batch_id)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=bool(result["result"]["ok"]), message=message, entry=result["entry"])


@router.post("/{entry_id}/late-partitions", response_model=ActionResult)
def late_partition(entry_id: int, payload: EntryPayload) -> ActionResult:
    """迟到分区补入：更新时间晚于批次的进入待复核，否则按分区当时口径补算。"""
    partition = str(payload.values.get("分区") or "").strip()
    updated_at = str(payload.values.get("数据更新时间") or "").strip()
    note = str(payload.values.get("备注") or payload.remark or "").strip()
    result, message = service.late_partition(entry_id, partition=partition, updated_at=updated_at, note=note)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=bool(result["result"]["ok"]), message=message, entry=result["entry"])


@router.post("/{entry_id}/reviews/resolve", response_model=ActionResult)
def resolve_reviews(entry_id: int, payload: EntryPayload) -> ActionResult:
    """复核迟到数据：确认不覆盖当前可消费版本后释放待复核状态。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=f"聚合流水 {entry_id} 不存在或已归档")
    note = str(payload.values.get("备注") or payload.remark or "").strip() or "确认不覆盖当前可消费版本"
    count = v.resolve_reviews(str(entry.get("输出表名")), note=note)
    refreshed = service.get_entry(entry_id)
    if count == 0:
        return ActionResult(ok=False, message="没有待复核的迟到数据", entry=refreshed)
    return ActionResult(ok=True, message=f"已复核 {count} 个分区的迟到数据", entry=refreshed)


@router.get("/{entry_id}/batches", response_model=dict)
def list_batches(entry_id: int) -> dict:
    """查看该聚合流水相关的重算批次与分区版本账本。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"聚合流水 {entry_id} 不存在或已归档")
    return {
        "批次": v.batches_for_table(str(entry.get("输出表名"))),
        "分区版本": [
            {**row, "状态": v.show_version_state(str(row.get("状态")))}
            for row in v.latest_partition_versions(str(entry.get("输出表名")))
        ],
        "待复核": v.pending_reviews(str(entry.get("输出表名"))),
    }
