"""口径版本、时间窗口与聚合批次：汇总层 / 应用层 / 事实定义共用一套重算状态。

内存版模型：
- `_fact_metric_versions`：事实定义每变更一次度量口径就登记一版（口径版本 + 生效时间）；
- `_dws_recalc_batches`：一次重算一个批次，批次按「汇总表 + 汇总粒度 + 时间窗口 + 口径版本」幂等；
- `_dws_partition_versions`：汇总表下每个分区当前保留的版本；新版核对通过前旧版继续可消费；
- `_dws_pending_reviews`：迟到分区进入待复核的记录，不覆盖已有可消费结果。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.store import store

VERSION_TABLE = "_fact_metric_versions"
BATCH_TABLE = "_dws_recalc_batches"
PARTITION_TABLE = "_dws_partition_versions"
REVIEW_TABLE = "_dws_pending_reviews"

GRAINS = ["小时", "日", "周", "月", "季", "年"]
_GRAIN_KEYWORDS = [
    ("小时", ("按小时", "小时", "hour")),
    ("日", ("按日", "日", "day", "date")),
    ("周", ("按周", "周", "week")),
    ("月", ("按月", "月", "month")),
    ("季", ("按季", "季", "quarter")),
    ("年", ("按年", "年", "year")),
]


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_time(value: Any) -> datetime | None:
    """把字段里的日期/日期时间解析为 datetime；识别不了返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H", "%Y-%m-%d %H", "%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def grain_from_text(*texts: Any) -> str | None:
    joined = " ".join(str(text or "").lower() for text in texts)
    for grain, keywords in _GRAIN_KEYWORDS:
        if any(keyword in joined for keyword in keywords):
            return grain
    return None


def fact_grain(fact: dict[str, Any] | None, entry: dict[str, Any] | None = None) -> str:
    """事实定义的分区字段决定事实粒度；缺失时退化到粒度说明/汇总粒度，最后按日兜底。"""
    field = str((fact or {}).get("分区字段") or "").strip().lower()
    if field:
        detected = grain_from_text(field)
        if detected:
            return detected
    detected = grain_from_text((fact or {}).get("粒度说明"), (entry or {}).get("汇总粒度"), (entry or {}).get("时间窗口"))
    return detected or "日"


def summary_grain(entry: dict[str, Any]) -> str:
    return grain_from_text(entry.get("汇总粒度"), entry.get("时间窗口")) or "日"


def normalize_window(text: Any, grain: str) -> str | None:
    """按粒度把时间窗口规范成幂等键写法。"""
    value = parse_time(text)
    if value is None:
        return None
    if grain == "小时":
        return value.strftime("%Y-%m-%dT%H")
    if grain == "月":
        return value.strftime("%Y-%m")
    if grain == "季":
        quarter = (value.month - 1) // 3 + 1
        return f"{value.year}-Q{quarter}"
    if grain == "年":
        return value.strftime("%Y")
    return value.strftime("%Y-%m-%d")


def advance(value: datetime, grain: str) -> datetime:
    if grain == "小时":
        return value + timedelta(hours=1)
    if grain == "周":
        return value + timedelta(days=7)
    months = {"月": 1, "季": 3, "年": 12}.get(grain, 0)
    if months:
        year = value.year + (value.month - 1 + months) // 12
        month = (value.month - 1 + months) % 12 + 1
        return value.replace(year=year, month=month, day=1, hour=0, minute=0, second=0)
    return value + timedelta(days=1)


def partition_end(window: Any, grain: str) -> datetime | None:
    """分区的结束时间：用于给批次安排一个与时间窗口一致的运行时刻。"""
    start = parse_time(window)
    if start is None:
        return None
    if grain in ("月", "季", "年"):
        start = start.replace(day=1, hour=0)
    return advance(start, grain)


def batch_times(windows: list[str], grain: str) -> tuple[str, str]:
    """模拟批次时间：窗口结束后 1 小时创建、1.5 小时完成，让迟到数据的先后判断可复现。"""
    ends = [partition_end(window, grain) for window in windows]
    ends = [end for end in ends if end is not None]
    if not ends:
        current = datetime.now().replace(microsecond=0)
        return current.strftime("%Y-%m-%d %H:%M:%S"), (current + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    end = max(ends)
    created = end + timedelta(hours=1)
    finished = end + timedelta(hours=1, minutes=30)
    return created.strftime("%Y-%m-%d %H:%M:%S"), finished.strftime("%Y-%m-%d %H:%M:%S")


def _window_bounds(entry: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """按汇总粒度解析时间窗口的起止；窗口内再按目标粒度展开。"""
    grain = summary_grain(entry)
    start = parse_time(entry.get("时间窗口"))
    if start is None:
        return None
    if grain == "小时":
        return start, start + timedelta(hours=1)
    if grain == "周":
        return start, start + timedelta(days=7)
    if grain in ("月", "季", "年"):
        return start.replace(day=1), advance(start.replace(day=1), grain)
    return start, start + timedelta(days=1)


def partitions_in_window(entry: dict[str, Any], grain: str) -> list[str]:
    """把聚合流水的时间窗口按给定粒度展开成有序分区列表。"""
    bounds = _window_bounds(entry)
    if bounds is None:
        return []
    start, end = bounds
    cursor = start.replace(day=1, hour=0) if grain in ("月", "季", "年") else start
    partitions: list[str] = []
    guard = 0
    while cursor < end and guard < 4000:
        partitions.append(normalize_window(cursor, grain) or "")
        cursor = advance(cursor, grain)
        guard += 1
    return [item for item in partitions if item]


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _split_metrics(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        raw = str(value or "").replace("，", ",").replace("、", ",").split(",")
    return [item.strip() for item in raw if item.strip()]


def _version_rank(version: Any) -> tuple[int, int]:
    text = str(version or "")
    try:
        tail = text.rsplit("-v", 1)[-1]
        major, minor = tail.split(".", 1)
        return int(major), int(minor)
    except (ValueError, IndexError):
        return 0, 0


# ---------------------------------------------------------------- 事实度量口径


def metric_versions(fact_no: str) -> list[dict[str, Any]]:
    return [row for row in store.rows(VERSION_TABLE) if row.get("事实编号") == fact_no]


def effective_metric_version(fact_no: str, at_time: Any = None) -> dict[str, Any] | None:
    """取某个时间点已生效的口径版本；不传时间则取当前最新一版。"""
    versions = metric_versions(fact_no)
    if not versions:
        return None
    anchor = parse_time(at_time) if at_time else None
    if anchor is not None:
        candidates = [row for row in versions if (parse_time(row.get("生效时间")) or anchor) <= anchor]
    else:
        candidates = versions
    if not candidates:
        candidates = versions
    return max(candidates, key=lambda row: (str(row.get("生效时间")), int(row.get("id", 0))))


def current_metric_version(fact: dict[str, Any] | None) -> dict[str, Any] | None:
    if not fact:
        return None
    return effective_metric_version(str(fact.get("事实编号") or ""))


def _next_version_no(fact_no: str) -> str:
    existing = metric_versions(fact_no)
    minor = 0
    for row in existing:
        minor = max(minor, _version_rank(row.get("口径版本"))[1])
    return f"{fact_no}-v1.{minor + 1}"


def changed_metrics(fact: dict[str, Any] | None) -> list[str]:
    """当前版本与上一版之间发生增删的度量字段。"""
    if not fact:
        return []
    versions = sorted(metric_versions(str(fact.get("事实编号") or "")), key=lambda row: int(row.get("id", 0)))
    if len(versions) < 2:
        return []
    previous = _split_metrics(versions[-2].get("度量字段"))
    current = _split_metrics(versions[-1].get("度量字段"))
    return sorted(set(previous) ^ set(current))


def register_metric_change(
    fact: dict[str, Any],
    *,
    metrics: Any,
    partition_field: str | None = None,
    effective_at: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    """登记一版新的度量口径：返回新版本记录，并同步事实定义上的当前口径。"""
    fact_no = str(fact.get("事实编号") or "").strip()
    if fact.get("status") != "已生效":
        raise ValueError(f"事实定义 {fact_no} 尚未发布（当前状态：{fact.get('status')}），不能修改度量口径")
    new_metrics = _split_metrics(metrics)
    if not new_metrics:
        raise ValueError("度量字段不能为空，请按逗号分隔填写")
    old_metrics = _split_metrics(fact.get("度量字段"))
    field = str(partition_field or fact.get("分区字段") or "").strip()
    grain = grain_from_text(field) or grain_from_text(fact.get("粒度说明")) or "日"
    effective = str(effective_at or now_text())
    if parse_time(effective) is None:
        raise ValueError("口径生效时间无法解析，请使用 YYYY-MM-DD HH:MM:SS 格式")
    current = current_metric_version(fact)
    if current is not None and new_metrics == old_metrics and field == str(fact.get("分区字段") or ""):
        # 同一口径重复提交：不登记新版本，下游无需重算。
        current["重复提交"] = True
        return {**current, "未变化": True}
    version = _next_version_no(fact_no)
    rows = store.rows(VERSION_TABLE)
    added = [item for item in new_metrics if item not in old_metrics]
    removed = [item for item in old_metrics if item not in new_metrics]
    record = {
        "id": _next_id(rows),
        "事实编号": fact_no,
        "事实名称": fact.get("事实名称"),
        "口径版本": version,
        "粒度": grain,
        "度量字段": ",".join(new_metrics),
        "分区字段": field,
        "生效时间": effective,
        "登记时间": now_text(),
        "变更度量": "；".join(
            part for part in (
                f"新增：{'、'.join(added)}" if added else "",
                f"移除：{'、'.join(removed)}" if removed else "",
            ) if part
        ) or "度量口径未变化，仅重新发布",
        "说明": note,
    }
    rows.append(record)
    fact["度量字段"] = ",".join(new_metrics)
    fact["分区字段"] = field
    fact["口径版本"] = version
    fact["口径生效时间"] = effective
    fact["粒度说明"] = f"按{grain}"
    fact["最近刷新"] = effective
    fact["最近变更度量"] = record["变更度量"]
    fact["待重算"] = True
    return record


def downstream_entries(fact: dict[str, Any]) -> list[dict[str, Any]]:
    """顺着聚合流水到事实定义的取数链路：事实编号优先，退化到输入表名 = 事实源表。"""
    fact_no = str(fact.get("事实编号") or "").strip()
    source = str(fact.get("源表名称") or "").strip()
    rows: list[dict[str, Any]] = []
    for entry in store.rows("dws"):
        linked = str(entry.get("事实编号") or "").strip()
        if (linked and linked == fact_no) or (not linked and source and str(entry.get("输入表名") or "").strip() == source):
            rows.append(entry)
    return rows


def mark_downstream(fact: dict[str, Any]) -> list[dict[str, Any]]:
    """口径变更后把下游聚合流水标成待重算，并给出各自受影响窗口。"""
    entries = downstream_entries(fact)
    for entry in entries:
        plan = plan_recalculation(entry, fact)
        entry["待重算"] = True
        entry["重算状态"] = "待重算" if plan["受影响"] else "无需重算"
        entry["受影响窗口"] = ",".join(plan["受影响窗口"] if plan["受影响"] else [])
        entry["口径版本"] = plan["事实口径版本"]
        entry["pending"] = bool(plan["受影响"] and plan["待处理分区"])
        entry["abnormal"] = entry["pending"]
    return entries


def sync_entry_flags(entry: dict[str, Any], fact: dict[str, Any] | None) -> None:
    """把重算状态同步回原始记录，供运营概览的待处理/异常量统计。"""
    table = str(entry.get("输出表名"))
    plan = plan_recalculation(entry, fact)
    reviews = pending_reviews(table)
    if reviews:
        entry["重算状态"] = "待复核"
    elif plan["批次"] and plan["批次"].get("状态") in ("待核对", "重算中"):
        entry["重算状态"] = plan["批次"].get("状态")
    elif plan["受影响"] and plan["待处理分区"]:
        entry["重算状态"] = "待重算"
    elif plan.get("首次产出") and plan["待处理分区"]:
        entry["重算状态"] = "首次产出"
    elif plan["变更度量"] and not plan["已生效分区"]:
        entry["重算状态"] = "无需重算"
    else:
        entry["重算状态"] = "已生效"
    entry["pending"] = bool(
        reviews or plan["待处理分区"] or (plan["批次"] and plan["批次"].get("状态") in ("待核对", "重算中"))
    )
    entry["abnormal"] = bool(reviews or (plan["受影响"] and plan["待处理分区"]))


def linked_fact(entry: dict[str, Any]) -> dict[str, Any] | None:
    fact_no = str(entry.get("事实编号") or "").strip()
    if fact_no:
        for row in store.rows("factdef"):
            if str(row.get("事实编号") or "").strip() == fact_no:
                return row
    source = str(entry.get("输入表名") or "").strip()
    if source:
        for row in store.rows("factdef"):
            if str(row.get("源表名称") or "").strip() == source:
                return row
    return None


# ---------------------------------------------------------------- 汇总表分区版本


def partition_versions(table: str) -> list[dict[str, Any]]:
    return [row for row in store.rows(PARTITION_TABLE) if row.get("汇总表") == table]


def latest_partition_versions(table: str) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in partition_versions(table):
        key = str(row.get("分区"))
        if key not in latest or int(row.get("id", 0)) > int(latest[key].get("id", 0)):
            latest[key] = row
    return sorted(latest.values(), key=lambda row: str(row.get("分区")))


def show_version_state(state: str) -> str:
    """对外展示时把内部状态翻译成可读的当前版本状态。"""
    if state == "已补入":
        return "已补入当前版本"
    if state == "待核对":
        return "待核对（当前仍消费上一版）"
    return state


def _row_count(entry: dict[str, Any], partition: str) -> int:
    """稳定的模拟行数：同一汇总表同一分区口径变化前后行数一致，便于核对覆盖范围。"""
    seed = sum(ord(char) for char in f"{entry.get('输出表名')}|{partition}")
    return 1000 + (seed % 5000)


def record_partition_version(
    entry: dict[str, Any],
    *,
    partition: str,
    version: str,
    state: str,
    batch_no: str,
    note: str = "",
) -> dict[str, Any]:
    rows = store.rows(PARTITION_TABLE)
    record = {
        "id": _next_id(rows),
        "聚合流水": entry.get("流水编号"),
        "汇总表": entry.get("输出表名"),
        "分区": partition,
        "口径版本": version,
        "状态": state,
        "批次编号": batch_no,
        "行数": _row_count(entry, partition),
        "说明": note,
        "更新时间": now_text(),
    }
    rows.append(record)
    return record


def active_version(table: str, partition: str) -> dict[str, Any] | None:
    """分区当前仍可消费的版本（已生效优先，否则回退上一版本）。"""
    rows = [
        row for row in partition_versions(table)
        if str(row.get("分区")) == partition and row.get("状态") in ("已生效", "上一版本")
    ]
    if not rows:
        return None
    effective = [row for row in rows if row.get("状态") == "已生效"]
    pool = effective or rows
    return max(pool, key=lambda row: int(row.get("id", 0)))


def batch_at(table: str, partition: str) -> dict[str, Any] | None:
    """分区当前可消费版本对应的批次（含完成时间，用于迟到数据的先后判断）。"""
    current = active_version(table, partition)
    batch_no = str((current or {}).get("批次编号") or "")
    for row in store.rows(BATCH_TABLE):
        if row.get("批次编号") == batch_no and row.get("汇总表") == table:
            return row
    return None


def consumable(table: str) -> dict[str, Any]:
    """应用层当前可消费版本：每个分区取当前可消费的版本（已生效优先，否则沿用上一版本）。"""
    rows = partition_versions(table)
    by_partition: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_partition.setdefault(str(row.get("分区")), []).append(row)
    chosen: list[dict[str, Any]] = []
    for partition, candidates in by_partition.items():
        effective = [item for item in candidates if item.get("状态") == "已生效"]
        merged = [item for item in candidates if item.get("状态") == "已补入"]
        previous = [item for item in candidates if item.get("状态") == "上一版本"]
        if effective:
            chosen.append(max(effective, key=lambda item: int(item.get("id", 0))))
        elif merged:
            # 迟到补算的旧口径结果已并入当前版本，当前消费版本由同口径的已生效/上一版本兜底。
            same_version = [
                item for item in candidates
                if item.get("状态") in ("已生效", "上一版本")
                and str(item.get("口径版本")) == str(merged[-1].get("口径版本"))
            ]
            if same_version:
                chosen.append(max(same_version, key=lambda item: int(item.get("id", 0))))
        elif previous:
            chosen.append(max(previous, key=lambda item: int(item.get("id", 0))))
    if not chosen:
        return {
            "可消费版本": None,
            "可消费窗口": "",
            "可消费状态": "尚无可消费版本",
            "分区版本": rows,
        }
    best = max(chosen, key=lambda row: int(row.get("id", 0)))
    same_version = [row for row in chosen if str(row.get("口径版本")) == str(best.get("口径版本"))]
    stale = [row for row in chosen if row not in same_version]
    status = "已生效" if any(row.get("状态") == "已生效" for row in same_version) else "沿用上一版本"
    detail = "已生效" if not stale else f"部分窗口沿用上一版本（{','.join(str(row.get('分区')) for row in stale)}）"
    return {
        "可消费版本": str(best.get("口径版本")),
        "可消费窗口": ",".join(str(row.get("分区")) for row in same_version),
        "可消费状态": status,
        "可消费明细": detail,
        "沿用上一版本的窗口": ",".join(str(row.get("分区")) for row in stale),
        "分区版本": rows,
    }


# ---------------------------------------------------------------- 重算批次


def _batches(entry: dict[str, Any]) -> list[dict[str, Any]]:
    flow = entry.get("流水编号")
    return [row for row in store.rows(BATCH_TABLE) if row.get("聚合流水") == flow]


def latest_batch(entry: dict[str, Any]) -> dict[str, Any] | None:
    rows = _batches(entry)
    if not rows:
        return None
    return max(rows, key=lambda row: (parse_time(row.get("创建时间")) or datetime.min, int(row.get("id", 0))))


def latest_metric_batch(entry: dict[str, Any]) -> dict[str, Any] | None:
    """只取口径变更触发的批次：迟到数据补算批次不能抬高「更新时间晚于批次」的判断门槛。"""
    rows = [row for row in _batches(entry) if row.get("触发原因") == "口径变更重算"]
    if not rows:
        return None
    return max(rows, key=lambda row: (parse_time(row.get("创建时间")) or datetime.min, int(row.get("id", 0))))


def find_batch(batch_id: int) -> dict[str, Any] | None:
    return store.find(BATCH_TABLE, batch_id)


def batches_for_table(table: str) -> list[dict[str, Any]]:
    return sorted(
        (row for row in store.rows(BATCH_TABLE) if row.get("汇总表") == table),
        key=lambda row: (parse_time(row.get("创建时间")) or datetime.min, int(row.get("id", 0))),
        reverse=True,
    )


def _batch_scope_key(entry: dict[str, Any], *, grain: str, window: str, version: str) -> str:
    return f"{entry.get('流水编号')}|{entry.get('输出表名')}|{grain}|{window}|{version}"


def _is_partition_done(table: str, partition: str, version: str) -> bool:
    return any(
        str(row.get("分区")) == partition and row.get("口径版本") == version and row.get("状态") == "已生效"
        for row in partition_versions(table)
    )


def produced_windows(entry: dict[str, Any], scope: list[str], grain: str) -> list[str]:
    """已产出的汇总窗口：窗口内存在已生效/上一版本分区，且对应的汇总批次已经完成。"""
    table = str(entry.get("输出表名"))
    rows = partition_versions(table)
    done_partitions = {
        str(row.get("分区")) for row in rows if row.get("状态") in ("已生效", "上一版本")
    }
    produced: list[str] = []
    for window in scope:
        inside = partitions_in_window(
            {"汇总粒度": f"按{grain}", "时间窗口": window}, grain
        ) or [window]
        if any(part in done_partitions for part in inside):
            produced.append(window)
    return produced


def plan_recalculation(entry: dict[str, Any], fact: dict[str, Any] | None) -> dict[str, Any]:
    """按事实粒度、度量和分区字段判断受影响窗口，再用汇总粒度与时间窗口界定重算范围。"""
    grain = summary_grain(entry)
    window = normalize_window(entry.get("时间窗口"), grain) or str(entry.get("时间窗口") or "")
    scope = partitions_in_window(entry, grain)
    fact_scope = partitions_in_window(entry, fact_grain(fact, entry))
    version_row = current_metric_version(fact)
    version = str(version_row.get("口径版本")) if version_row else str((fact or {}).get("口径版本") or "未关联口径")
    changed = changed_metrics(fact)
    aggregated = _split_metrics(entry.get("聚合字段"))
    fact_metrics = _split_metrics((fact or {}).get("度量字段"))
    affected = True
    reason = "按汇总粒度与时间窗口确定重算范围"
    if fact is not None and changed:
        hit = [item for item in changed if item in aggregated]
        missing = [item for item in aggregated if item not in fact_metrics]
        if not hit and not missing:
            affected = False
            reason = f"事实口径变更（{'、'.join(changed)}）未触及本流水聚合字段（{'、'.join(aggregated)}）"
        else:
            reason = f"事实口径变更命中聚合字段：{'、'.join(hit or missing)}"
    table = str(entry.get("输出表名"))
    pending = [partition for partition in scope if not _is_partition_done(table, partition, version)]
    done = [partition for partition in scope if partition not in pending]
    produced = produced_windows(entry, scope, grain) if fact is not None and changed else []
    hit_metrics: list[str] = []
    if fact is not None and changed:
        hit_metrics = [item for item in changed if item in aggregated]
    first_production = bool(pending) and not produced
    key = _batch_scope_key(entry, grain=grain, window=window, version=version)
    batch = next((row for row in _batches(entry) if row.get("幂等键") == key), None)
    if hit_metrics and not produced:
        affected = False
        reason = (
            f"事实口径变更（{'、'.join(changed)}）命中聚合字段，"
            f"但时间窗口 {window} 尚未产出聚合结果，按首次产出处理"
        )
    return {
        "汇总粒度": grain,
        "时间窗口": window,
        "时间窗口分区": scope,
        "受影响窗口": produced,
        "已产出窗口": produced,
        "事实粒度": fact_grain(fact, entry),
        "事实分区": fact_scope,
        "事实口径版本": version,
        "口径生效时间": version_row.get("生效时间") if version_row else None,
        "变更度量": changed,
        "命中度量": hit_metrics,
        "事实度量": fact_metrics,
        "聚合字段": aggregated,
        "受影响": affected,
        "首次产出": first_production,
        "判断依据": reason,
        "待处理分区": pending,
        "已生效分区": done,
        "幂等键": key,
        "批次": batch,
    }


def recalculate(entry: dict[str, Any], fact: dict[str, Any] | None, *, note: str = "") -> dict[str, Any]:
    """同一时间窗口 + 同一口径版本重复提交时复用原批次，不重复登记分区。"""
    plan = plan_recalculation(entry, fact)
    version = plan["事实口径版本"]
    if not plan["时间窗口"]:
        return {"ok": False, "message": "聚合流水缺少可解析的时间窗口，无法确定重算范围", "plan": plan, "batch": None}
    existing = plan["批次"]
    if existing is not None:
        return {
            "ok": True,
            "message": f"同一时间窗口与口径版本已提交过批次 {existing.get('批次编号')}，复用原批次，本次不重复重算",
            "plan": plan,
            "batch": existing,
            "idempotent": True,
        }
    if not plan["受影响"] and not plan.get("首次产出"):
        entry["待重算"] = False
        entry["重算状态"] = "无需重算"
        return {"ok": True, "message": plan["判断依据"], "plan": plan, "batch": None, "idempotent": True}
    pending = plan["待处理分区"]
    if not pending:
        entry["待重算"] = False
        entry["重算状态"] = "已生效"
        entry["可消费版本"] = version
        return {
            "ok": True,
            "message": f"时间窗口内分区已按 {version} 生效，无需重算",
            "plan": plan,
            "batch": None,
            "idempotent": True,
        }
    rows = store.rows(BATCH_TABLE)
    batch_no = f"RC-{_next_id(rows):06d}"
    trigger = "首次产出" if plan.get("首次产出") else "口径变更重算"
    created, finished = batch_times([plan["时间窗口"]], str(plan["汇总粒度"]))
    batch = {
        "id": _next_id(rows),
        "批次编号": batch_no,
        "聚合流水": entry.get("流水编号"),
        "汇总表": entry.get("输出表名"),
        "事实编号": (fact or {}).get("事实编号"),
        "汇总粒度": plan["汇总粒度"],
        "时间窗口": plan["时间窗口"],
        "事实粒度": plan["事实粒度"],
        "口径版本": version,
        "幂等键": plan["幂等键"],
        "触发原因": trigger,
        "状态": "待核对",
        "待处理分区": list(pending),
        "已跳过分区": list(plan["已生效分区"]),
        "待核对分区": list(pending),
        "待复核分区": [],
        "创建时间": created,
        "完成时间": None,
        "核对结果": None,
        "核对说明": None,
        "重算次数": 1,
        "计划完成时间": finished,
        "说明": note or (
            f"时间窗口 {plan['时间窗口']} 首次产出，按口径 {version} 生成"
            if trigger == "首次产出"
            else f"事实口径 {version} 生效后重算 {plan['时间窗口']} 窗口"
        ),
    }
    rows.append(batch)
    table = str(entry.get("输出表名"))
    for partition in pending:
        record_partition_version(
            entry,
            partition=partition,
            version=version,
            state="待核对",
            batch_no=batch_no,
            note="新版本已产出，等待一致性核对；旧结果继续作为可消费版本",
        )
    entry["待重算"] = False
    entry["重算状态"] = "待核对"
    entry["最近批次"] = batch_no
    entry["口径版本"] = version
    entry["受影响窗口"] = ",".join(plan["受影响窗口"])
    return {"ok": True, "message": f"已提交 {batch_no}，仅重算 {len(pending)} 个待处理分区", "plan": plan, "batch": batch}


def verify_batch(entry: dict[str, Any], fact: dict[str, Any] | None, batch: dict[str, Any]) -> dict[str, Any]:
    """一致性核对：覆盖、行数、口径三关都通过，才把新版本切为可消费版本。"""
    if batch.get("状态") == "已生效":
        return {"ok": True, "message": f"批次 {batch.get('批次编号')} 已生效，无需重复核对", "batch": batch, "idempotent": True}
    version = str(batch.get("口径版本") or "")
    table = str(entry.get("输出表名"))
    records = [
        row for row in store.rows(PARTITION_TABLE)
        if row.get("汇总表") == table and row.get("批次编号") == batch.get("批次编号")
    ]
    covered = {str(row.get("分区")): row for row in records}
    partitions = [str(item) for item in batch.get("待核对分区") or []]
    issues: list[str] = []
    for partition in partitions:
        row = covered.get(partition)
        if row is None:
            issues.append(f"分区 {partition} 缺少新版本结果")
            continue
        if row.get("状态") != "待核对":
            issues.append(f"分区 {partition} 当前状态为「{row.get('状态')}」，不能直接切为可消费版本")
            continue
        if int(row.get("行数", 0)) <= 0:
            issues.append(f"分区 {partition} 行数为 0")
        previous = [
            item for item in partition_versions(table)
            if str(item.get("分区")) == partition
            and int(item.get("id", 0)) < int(row.get("id", 0))
            and int(item.get("行数", 0)) > 0
        ]
        if previous:
            old = max(previous, key=lambda item: int(item.get("id", 0)))
            old_count = int(old.get("行数", 0))
            if old_count and abs(int(row.get("行数", 0)) - old_count) / old_count > 0.1:
                issues.append(f"分区 {partition} 行数与上一版本偏差超过 10%")
    trigger = str(batch.get("触发原因") or "")
    review_partitions = {
        str(row.get("分区")) for row in pending_reviews(table)
    }
    for partition in partitions:
        if partition in review_partitions:
            issues.append(f"分区 {partition} 存在待复核的迟到数据，复核完成前不能切换新版本")
    if trigger == "口径变更重算" and fact is not None:
        current = current_metric_version(fact)
        if current and str(current.get("口径版本")) != version:
            issues.append(
                f"事实口径已更新为 {current.get('口径版本')}，批次 {batch.get('批次编号')} 依据的 {version} 已过期"
            )
    if issues:
        batch["核对结果"] = "未通过"
        batch["核对说明"] = "；".join(issues)
        batch["状态"] = "待核对"
        entry["重算状态"] = "待核对"
        return {"ok": False, "message": f"一致性核对未通过：{'；'.join(issues)}", "batch": batch}
    for partition in partitions:
        new_row = covered[partition]
        previous = max(
            (
                old for old in partition_versions(table)
                if str(old.get("分区")) == partition and int(old.get("id", 0)) < int(new_row.get("id", 0))
            ),
            key=lambda old: int(old.get("id", 0)),
            default=None,
        )
        current = active_version(table, partition)
        # 迟到补算按分区当时口径重算；若当前可消费版本更新，则只把补算结果并入，
        # 不把已消费的新口径版本回退成旧口径。
        if current is not None and _version_rank(new_row.get("口径版本")) < _version_rank(current.get("口径版本")):
            new_row["状态"] = "已补入"
            new_row["说明"] = (
                f"迟到补算按分区当时口径 {new_row.get('口径版本')} 完成，已并入当前可消费版本 "
                f"{current.get('口径版本')}，不回退已消费版本"
            )
            continue
        if previous is not None:
            # 新版只替换「最近一版」，更早的版本降为历史；上一版本保留给应用层回退消费。
            for old in partition_versions(table):
                if str(old.get("分区")) != partition or int(old.get("id", 0)) >= int(new_row.get("id", 0)):
                    continue
                if old is previous:
                    old["状态"] = "上一版本"
                elif old.get("状态") in ("已生效", "上一版本"):
                    old["状态"] = "历史版本"
        new_row["状态"] = "已生效"
        new_row["说明"] = "一致性核对通过，切为当前可消费版本"
    merged = [partition for partition in partitions if covered[partition].get("状态") == "已补入"]
    batch["状态"] = "已生效"
    batch["完成时间"] = batch.get("计划完成时间") or now_text()
    batch["核对结果"] = "通过"
    batch["核对说明"] = f"覆盖 {len(partitions)} 个分区，行数与口径核对通过"
    if merged:
        batch["核对说明"] += f"；{','.join(merged)} 为旧口径迟到补算，已并入当前可消费版本，未回退"
    entry["重算状态"] = "已生效"
    entry["pending"] = False
    entry["可消费版本"] = version
    entry["最近批次"] = batch.get("批次编号")
    message = f"批次 {batch.get('批次编号')} 核对通过"
    if merged:
        message += f"；{'、'.join(merged)} 的补算结果已并入当前可消费版本，不回退已消费口径"
    else:
        message += f"，{version} 已切为可消费版本，旧版本保留为上一版本"
    return {"ok": True, "message": message, "batch": batch}


def _summary_windows_for_partition(entry: dict[str, Any], partition: str, fact: dict[str, Any] | None) -> list[str]:
    """按事实粒度与汇总粒度，把迟到分区映射到受影响的汇总窗口。"""
    summary = summary_grain(entry)
    fgrain = fact_grain(fact, entry)
    value = parse_time(partition)
    if value is None:
        return []
    if summary == fgrain:
        return [normalize_window(value, summary) or partition]
    rank = {grain: index for index, grain in enumerate(GRAINS)}
    if rank.get(fgrain, 0) > rank.get(summary, 0):
        # 事实粒度比汇总粒度更粗：该事实分区覆盖多个汇总窗口。
        return partitions_in_window(
            {"汇总粒度": f"按{fgrain}", "时间窗口": normalize_window(value, fgrain)},
            summary,
        ) or [normalize_window(value, summary) or partition]
    return [normalize_window(value, summary) or partition]


def register_late_partition(
    entry: dict[str, Any],
    fact: dict[str, Any] | None,
    *,
    partition: str,
    updated_at: str,
    note: str = "",
) -> dict[str, Any]:
    """迟到分区：更新时间晚于批次的进入待复核；不晚于批次的按分区当时口径补算。"""
    if parse_time(updated_at) is None:
        return {"ok": False, "message": "数据更新时间无法解析，请使用 YYYY-MM-DD HH:MM:SS 格式", "reviews": [], "batch": None}
    windows = _summary_windows_for_partition(entry, partition, fact) or [str(partition)]
    table = str(entry.get("输出表名"))
    # 判断基准是当前可消费结果所属的聚合批次（而不是最近一次补算批次），
    # 这样迟到数据永远和「已经产出并被下游消费的结果」比较先后。
    batch = max(
        (batch_at(table, window) for window in windows if batch_at(table, window) is not None),
        key=lambda row: (parse_time(row.get("完成时间") or row.get("创建时间")) or datetime.min, int(row.get("id", 0))),
        default=None,
    ) or latest_metric_batch(entry) or latest_batch(entry)
    batch_time = parse_time((batch or {}).get("完成时间") or (batch or {}).get("创建时间"))
    data_time = parse_time(updated_at)
    fact_no = str((fact or {}).get("事实编号") or "")
    version_row = effective_metric_version(fact_no, partition) if fact_no else None
    version = str(version_row.get("口径版本")) if version_row else str((fact or {}).get("口径版本") or "未关联口径")
    if batch_time is not None and data_time is not None and data_time > batch_time:
        reviews: list[dict[str, Any]] = []
        already = any(
            str(row.get("分区")) in windows and str(row.get("状态")) == "待复核"
            for row in store.rows(REVIEW_TABLE)
            if row.get("汇总表") == table
        )
        if already:
            return {
                "ok": True,
                "message": f"分区 {','.join(windows)} 已有待复核记录，未重复登记，也未覆盖现有结果",
                "reviews": [row for row in pending_reviews(table) if str(row.get("分区")) in windows],
                "batch": batch,
                "idempotent": True,
            }
        for window in windows:
            record = {
                "id": _next_id(store.rows(REVIEW_TABLE)),
                "聚合流水": entry.get("流水编号"),
                "汇总表": entry.get("输出表名"),
                "分区": window,
                "口径版本": version,
                "数据更新时间": updated_at,
                "批次时间": batch.get("完成时间") or batch.get("创建时间"),
                "原因": "数据更新时间晚于聚合批次，结果不静默覆盖",
                "说明": note,
                "状态": "待复核",
                "登记时间": now_text(),
            }
            store.rows(REVIEW_TABLE).append(record)
            reviews.append(record)
            batch.setdefault("待复核分区", [])
            if window not in batch["待复核分区"]:
                batch["待复核分区"].append(window)
            record_partition_version(
                entry,
                partition=window,
                version=version,
                state="待复核",
                batch_no=str(batch.get("批次编号")),
                note=f"迟到数据 {updated_at} 晚于批次 {batch.get('完成时间') or batch.get('创建时间')}，进入待复核",
            )
        entry["重算状态"] = "待复核"
        entry["待复核分区"] = ",".join(windows)
        entry["pending"] = True
        return {
            "ok": True,
            "message": f"分区 {','.join(windows)} 的迟到数据晚于聚合批次，已进入待复核，未覆盖现有结果",
            "reviews": reviews,
            "batch": batch,
        }
    key = f"{entry.get('流水编号')}|{entry.get('输出表名')}|迟到补算|{','.join(windows)}|{version}"
    for row in _batches(entry):
        if row.get("幂等键") == key and row.get("触发原因") == "迟到数据补算":
            return {"ok": True, "message": f"该迟到分区已提交过补算批次 {row.get('批次编号')}，复用原批次", "batch": row, "reviews": [], "idempotent": True}
    if any(str(row.get("分区")) in windows for row in pending_reviews(table)):
        return {
            "ok": False,
            "message": f"分区 {','.join(windows)} 已按当时生效口径补算过，请先对该补算结果做一致性核对",
            "reviews": [row for row in pending_reviews(table) if str(row.get("分区")) in windows],
            "batch": batch,
        }
    rows = store.rows(BATCH_TABLE)
    batch_no = f"RC-{_next_id(rows):06d}"
    created, finished = batch_times(windows, summary_grain(entry))
    new_batch = {
        "id": _next_id(rows),
        "批次编号": batch_no,
        "聚合流水": entry.get("流水编号"),
        "汇总表": entry.get("输出表名"),
        "事实编号": (fact or {}).get("事实编号"),
        "汇总粒度": summary_grain(entry),
        "时间窗口": ",".join(windows),
        "事实粒度": fact_grain(fact, entry),
        "口径版本": version,
        "幂等键": key,
        "触发原因": "迟到数据补算",
        "状态": "待核对",
        "待处理分区": list(windows),
        "已跳过分区": [],
        "待核对分区": list(windows),
        "待复核分区": [],
        "创建时间": created,
        "完成时间": None,
        "核对结果": None,
        "核对说明": None,
        "重算次数": 1,
        "计划完成时间": finished,
        "说明": note or f"迟到数据按分区当时生效口径 {version} 补算",
    }
    rows.append(new_batch)
    for window in windows:
        # 补算结果先登记为待核对，旧结果继续可消费；核对时若当前消费版本更新，
        # 则补算结果并入当前版本而不回退已消费口径。
        record_partition_version(
            entry,
            partition=window,
            version=version,
            state="待核对",
            batch_no=batch_no,
            note=f"迟到数据按 {updated_at} 时已生效的口径 {version} 补算",
        )
    entry["重算状态"] = "待核对"
    entry["最近批次"] = batch_no
    return {
        "ok": True,
        "message": f"已按分区当时生效口径 {version} 提交补算批次 {batch_no}，旧结果保留至核对通过",
        "batch": new_batch,
        "reviews": [],
    }


def sync_ads_flags() -> None:
    """同步应用层原始记录的待复核/沿用上一版本标记，供概览统计消费。"""
    for row in store.rows("ads"):
        table = str(row.get("汇总表名") or row.get("输入表名") or "").strip()
        reviews = pending_reviews(table)
        state = consumable(table)
        row["待复核数量"] = len(reviews)
        row["消费状态"] = "待复核，暂不消费" if reviews else state["可消费状态"]
        row["pending"] = bool(reviews or state["可消费状态"] == "沿用上一版本")
        row["abnormal"] = bool(reviews)


def sync_all_flags() -> None:
    """写回各业务记录的派生状态：概览页、列表页读取同一口径。"""
    for entry in store.rows("dws"):
        sync_entry_flags(entry, linked_fact(entry))
    sync_ads_flags()


def pending_reviews(table: str) -> list[dict[str, Any]]:
    return [
        row for row in store.rows(REVIEW_TABLE)
        if row.get("汇总表") == table and row.get("状态") == "待复核"
    ]


def resolve_reviews(table: str, *, note: str = "") -> int:
    """复核待复核的迟到数据：复核完成后把它们从待复核状态释放，不影响已消费版本。"""
    rows = pending_reviews(table)
    for row in rows:
        row["状态"] = "已复核"
        row["复核说明"] = note
        row["复核时间"] = now_text()
        for version_row in store.rows(PARTITION_TABLE):
            if (
                version_row.get("汇总表") == table
                and str(version_row.get("分区")) == str(row.get("分区"))
                and version_row.get("状态") == "待复核"
            ):
                version_row["状态"] = "已复核"
                version_row["说明"] = f"迟到数据复核完成：{note or '确认不覆盖当前可消费版本'}"
                version_row["更新时间"] = now_text()
    return len(rows)


def entry_state(entry: dict[str, Any], fact: dict[str, Any] | None) -> dict[str, Any]:
    """聚合流水上的重算口径：受影响窗口、口径版本、重算状态与可消费版本。"""
    plan = plan_recalculation(entry, fact)
    table = str(entry.get("输出表名"))
    consume = consumable(table)
    batch = plan["批次"] or latest_metric_batch(entry) or latest_batch(entry)
    reviews = pending_reviews(table)
    status = "已生效"
    if reviews:
        status = "待复核"
    elif batch and batch.get("状态") in ("待核对", "重算中"):
        status = "待核对"
    elif plan["受影响"] and plan["待处理分区"]:
        status = "待重算"
    elif plan.get("首次产出") and plan["待处理分区"]:
        status = "首次产出"
    elif not plan["受影响"] and plan["变更度量"]:
        status = "无需重算"
    consume_status = str(consume.get("可消费明细") or consume.get("可消费状态") or "")
    return {
        "事实编号": (fact or {}).get("事实编号") or entry.get("事实编号"),
        "事实名称": (fact or {}).get("事实名称"),
        "事实口径版本": plan["事实口径版本"],
        "口径生效时间": plan["口径生效时间"],
        "变更度量": "、".join(plan["变更度量"]) or (fact or {}).get("最近变更度量") or "",
        "汇总粒度": plan["汇总粒度"],
        "受影响窗口": ",".join(plan["受影响窗口"]),
        "事实粒度": plan["事实粒度"],
        "事实分区": ",".join(plan["事实分区"]),
        "待处理分区": ",".join(plan["待处理分区"]),
        "已跳过分区": ",".join(plan["已生效分区"]),
        "判断依据": plan["判断依据"],
        "重算状态": status,
        "批次编号": (batch or {}).get("批次编号"),
        "批次ID": (batch or {}).get("id"),
        "批次状态": (batch or {}).get("状态"),
        "核对结果": (batch or {}).get("核对结果"),
        "可消费版本": consume["可消费版本"],
        "可消费窗口": consume["可消费窗口"],
        "可消费状态": consume_status,
        "沿用上一版本的窗口": consume.get("沿用上一版本的窗口", ""),
        "待复核分区": ",".join(str(row.get("分区")) for row in reviews),
        "待复核数量": len(reviews),
        "计划": plan,
    }
