"""导出编排：四份文件都从已落库的数据生成，保证与页面同源。

原始日值被 M8 清理后，readings 表为空但留档凭证仍在：读数证据与逐日台账
改从归档目录取（先过 sha256 校验），月报、区间、隔离、清单继续从库内
保留的月量/区间/隔离表生成，因此清理后导出仍能独立对账。
"""
from __future__ import annotations

from ... import errors
from ..m2_contract import contract as contract_mod
from ..m8_retention import service as retention_service
from . import csv_out, evidence, manifest_out


def _context(settings, store, batch_id: str | None) -> tuple[dict, str]:
    batch = store.get_batch(batch_id) if batch_id else store.latest_batch()
    if batch is None:
        raise errors.NotFound("还没有批次可以导出", code="E_BATCH_NOT_FOUND")
    return batch, batch["batch_id"]


def _archive_record(store, ident: str) -> dict | None:
    return store.get_archive(ident)


def monthly_csv(settings, store, batch_id: str | None = None) -> tuple[str, str]:
    batch, ident = _context(settings, store, batch_id)
    contracts = contract_mod.load(settings)
    allocations = store.load_allocations(ident)
    bills = store.load_bills(ident)
    body = csv_out.render(evidence.MONTHLY_HEADER,
                          evidence.monthly_rows(allocations, bills, contracts))
    return body, f"monthly-{ident}.csv"


def intervals_csv(settings, store, batch_id: str | None = None) -> tuple[str, str]:
    _, ident = _context(settings, store, batch_id)
    body = csv_out.render(evidence.INTERVAL_HEADER,
                          evidence.interval_rows(store.load_intervals(ident)))
    return body, f"intervals-{ident}.csv"


def quarantine_csv(settings, store, batch_id: str | None = None) -> tuple[str, str]:
    _, ident = _context(settings, store, batch_id)
    body = csv_out.render(evidence.QUARANTINE_HEADER,
                          evidence.quarantine_rows(store.load_quarantine(ident)))
    return body, f"quarantine-{ident}.csv"


def readings_csv(settings, store, batch_id: str | None = None) -> tuple[str, str]:
    _, ident = _context(settings, store, batch_id)
    if store.count_rows(ident)["readings"] == 0 and _archive_record(store, ident):
        # 原始日值已清理：回退到留档凭证，校验哈希后导出，内容与清理前逐位一致
        data, _ = retention_service.read_archived_file(
            settings, store, ident, retention_service.READINGS_NAME)
        return data.decode("utf-8"), f"readings-{ident}.csv"
    body = csv_out.render(evidence.READING_HEADER,
                          evidence.reading_rows(store.load_readings(ident)))
    return body, f"readings-{ident}.csv"


def daily_ledger_csv(settings, store,
                     batch_id: str | None = None) -> tuple[str, str]:
    """逐日裁决台账：清理后从留档凭证回取，未清理则现场生成，两者同源。"""
    _, ident = _context(settings, store, batch_id)
    record = _archive_record(store, ident)
    if record is not None:
        data, _ = retention_service.read_archived_file(
            settings, store, ident, retention_service.LEDGER_NAME)
        return data.decode("utf-8"), f"daily-ledger-{ident}.csv"
    from ..m8_retention import ledger
    totals = store.load_totals(ident)
    body = csv_out.render(
        ledger.LEDGER_HEADER,
        ledger.daily_ledger_rows(
            store.load_readings(ident), store.load_quarantine(ident),
            store.load_intervals(ident), store.load_allocations(ident)))
    return body, f"daily-ledger-{ident}.csv"


def batch_receipt_csv(settings, store,
                      batch_id: str | None = None) -> tuple[str, str]:
    """批次回执：每批收了多少条、各去向多少、差额多少，清理后照样可导出。"""
    batch, ident = _context(settings, store, batch_id)
    record = _archive_record(store, ident)
    if record is not None:
        data, _ = retention_service.read_archived_file(
            settings, store, ident, retention_service.RECEIPT_NAME)
        return data.decode("utf-8"), f"batch-receipt-{ident}.csv"
    import json
    from ..m8_retention import ledger
    totals = store.load_totals(ident)
    reconcile = json.loads(totals.get("reconcile_json") or "null")
    body = csv_out.render(
        ledger.RECEIPT_HEADER,
        ledger.receipt_rows(batch, totals, reconcile))
    return body, f"batch-receipt-{ident}.csv"


def manifest_json(settings, store, batch_id: str | None = None) -> tuple[str, str]:
    batch, ident = _context(settings, store, batch_id)
    payload = manifest_out.build(store, batch, store.load_manifest(ident))
    return manifest_out.render_json(payload), f"manifest-{ident}.json"


EXPORTS = {
    "monthly": ("月度分摊 CSV", monthly_csv, "text/csv; charset=utf-8"),
    "intervals": ("区间还原 CSV", intervals_csv, "text/csv; charset=utf-8"),
    "quarantine": ("隔离区 CSV", quarantine_csv, "text/csv; charset=utf-8"),
    "readings": ("读数证据 CSV", readings_csv, "text/csv; charset=utf-8"),
    "daily_ledger": ("逐日裁决台账 CSV", daily_ledger_csv, "text/csv; charset=utf-8"),
    "receipt": ("批次回执 CSV", batch_receipt_csv, "text/csv; charset=utf-8"),
    "manifest": ("运行清单 JSON", manifest_json, "application/json; charset=utf-8"),
}


def page_context(settings, store, batch) -> dict:
    if batch is None:
        return {"files": [], "manifest_summary": [], "counts": {},
                "archive": None}
    ident = batch["batch_id"]
    counts = store.count_rows(ident)
    archive = store.get_archive(ident)
    files = [
        {"key": "monthly", "label": "月度分摊 CSV", "href": "/api/export/monthly.csv",
         "rows": counts["allocations"]},
        {"key": "intervals", "label": "区间还原 CSV", "href": "/api/export/intervals.csv",
         "rows": counts["intervals"]},
        {"key": "quarantine", "label": "隔离区 CSV", "href": "/api/export/quarantine.csv",
         "rows": counts["quarantine"]},
        {"key": "readings", "label": "读数证据 CSV", "href": "/api/export/readings.csv",
         "rows": counts["readings"],
         "from_archive": archive is not None and counts["readings"] == 0},
        {"key": "daily_ledger", "label": "逐日裁决台账 CSV",
         "href": "/api/export/daily-ledger.csv",
         "rows": counts["readings"] + counts["quarantine"]},
        {"key": "receipt", "label": "批次回执 CSV",
         "href": "/api/export/batch-receipt.csv", "rows": 1},
        {"key": "manifest", "label": "运行清单 JSON", "href": "/api/export/manifest.json",
         "rows": 1},
    ]
    return {
        "files": files,
        "manifest_summary": manifest_out.summary(
            manifest_out.build(store, batch, store.load_manifest(ident))),
        "counts": counts,
        "archive": archive,
    }
