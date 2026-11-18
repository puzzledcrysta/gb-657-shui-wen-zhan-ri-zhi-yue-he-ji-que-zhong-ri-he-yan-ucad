"""对账台账：清理原始日值后，月报仍能凭这两份台账独立对账。

两份 CSV 回答两个问题：
- 批次回执（receipt）：每批收了多少条、采信/估抄/隔离/空行各多少，差额看得见；
- 逐日裁决台账（daily ledger）：原始每一行去了哪里、为什么采信或隔离、
  进了哪个区间与哪个月桶，抽查某一天能逐行解释。

台账只从已落库的批次数据生成，与页面、月报同源；不依赖原始来件文件，
所以 readings 表被清理后仍可重新导出、逐位核对。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from ... import config, reasons

# 批次回执：左 = 实际收到，右 = 各去向之和，差额单独成列
RECEIPT_HEADER = ["批次号", "输入文件", "输入sha256", "规则版本",
                  "收到数据行", "采信行", "估抄行", "隔离行", "空行",
                  "去向合计", "差额行", "读数账平衡", "区间数", "月度分摊行",
                  "账单数", "月用量kWh"]
# 逐日裁决：每个原始行号一行，隔离行没有区间/月桶也保留，说明为何没算进月量
LEDGER_HEADER = ["原始行号", "分段", "当地日期", "表号", "寄存器", "抄表时间UTC",
                 "原始读数", "单位", "折合读数", "类型", "裁决", "原因码", "原因",
                 "参与月量", "区间号", "归属月份", "说明"]


def _local_date(read_at_utc: str | None) -> str:
    """UTC 入库时间 → Asia/Shanghai 自然日，抽查按「哪一天」问，台账按天答。"""
    if not read_at_utc:
        return ""
    instant = dt.datetime.fromisoformat(read_at_utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=config.UTC)
    return instant.astimezone(config.CHINA_TZ).date().isoformat()


def _interval_index(intervals: list[dict]) -> dict[tuple, list[dict]]:
    index: dict[tuple, list[dict]] = {}
    for item in intervals:
        index.setdefault((item["meter_id"], item["register"]), []).append(item)
    return index


def _find_interval(intervals_index: dict, meter_id: str, register: str,
                   read_at: str) -> dict | None:
    """读数作为区间止点锚点：落在 [start, end) 末端的区间即它贡献用量的区间。"""
    for item in intervals_index.get((meter_id, register), []):
        if item["end_at"] == read_at:
            return item
    return None


def _allocation_month(allocations: list[dict], interval_id: str) -> str:
    months = sorted({
        item["month"] for item in allocations
        if interval_id in (item.get("sources") or [])
    })
    return ";".join(months)


def daily_ledger_rows(readings: list[dict], quarantine: list[dict],
                      intervals: list[dict], allocations: list[dict]) -> list[list[str]]:
    """每个原始行号一行：先列采信/估抄，再列隔离，按行号排序便于逐日抽查。"""
    intervals_index = _interval_index(intervals)
    rows: list[list[str]] = []

    for item in readings:
        interval = _find_interval(intervals_index, item["meter_id"],
                                  item["register"], item["read_at"])
        interval_id = interval["interval_id"] if interval else ""
        months = _allocation_month(allocations, interval_id) if interval_id else ""
        participates = "是" if interval_id else "否"
        note = ""
        if not interval_id:
            # 首读只作锚点不产生区间；估抄不作锚点——两者都不进月量，需要写清
            if item["read_type"] == "E":
                note = "估抄不作锚点，不参与月量"
            else:
                note = "序列首读，仅作起点不产生用量"
        norm = item.get("reading_norm")
        rows.append([
            str(item["lineno"]), str(item.get("segment") or 1),
            _local_date(item["read_at"]), item["meter_id"],
            config.REGISTER_LABELS.get(item["register"], item["register"]),
            item["read_at"], item["reading_text"], item.get("unit_raw") or "",
            "" if norm is None else f"{Decimal(str(norm)):.3f}",
            item["read_type"],
            reasons.label({"valid": "采信", "estimate": "估抄"}.get(
                item["status"], item["status"])),
            item.get("code") or "", item.get("code") and reasons.label(item["code"]) or "",
            participates, interval_id, months, note,
        ])

    for item in quarantine:
        # 隔离行没有 read_at 字段（隔离表只留原值与行号），日期/时间留空，
        # 列位与采信行严格对齐（17 列）：类型留空，裁决固定「隔离」，原因码即解释
        rows.append([
            str(item["lineno"]), str(item.get("segment") or 1), "",
            item.get("meter_id") or "", "", "", item.get("raw_value") or "",
            "", "", "", "隔离", item["code"], reasons.label(item["code"]),
            "否", "", "", item.get("detail") or "",
        ])

    rows.sort(key=lambda row: int(row[0]))
    return rows


def receipt_rows(batch: dict, totals: dict, reconcile: dict | None) -> list[list[str]]:
    """批次回执一行：收到 = 采信 + 估抄 + 隔离 + 空行，差额不为 0 时显式写出来。"""
    raw_lines = int(totals.get("raw_lines", 0))
    valid = int(totals.get("valid", 0))
    estimate = int(totals.get("estimate", 0))
    quarantined = int(totals.get("quarantine", 0))
    blank = int(totals.get("blank", 0))
    disposed = valid + estimate + quarantined + blank
    diff = raw_lines - disposed
    balanced = "是" if diff == 0 else "否"
    reading_account = next(
        (a for a in (reconcile or {}).get("accounts", [])
         if a.get("key") == "readings"), None)
    if reading_account is not None:
        balanced = "是" if reading_account.get("balanced") else "否"
    inputs = batch.get("input_digest", "")
    return [[
        batch["batch_id"], batch["sample"], inputs, batch["rule_version"],
        str(raw_lines), str(valid), str(estimate), str(quarantined), str(blank),
        str(disposed), str(diff), balanced,
        str(totals.get("interval_count", "")), str(totals.get("allocation_count", "")),
        str(totals.get("bill_count", "")), str(totals.get("month_usage", "")),
    ]]
