"""M8 留档清理：先归档后清理、凭证写一次、重试不覆盖、清理后月报仍可独立对账。

覆盖核心反例：
- 未留档就清理 → 拒绝；
- 重复留档 → 复用原凭证，文件字节与 mtime 不变；
- 凭证被篡改 → 校验/清理报 E_ARCHIVE_CORRUPT，不删一行原始日值；
- 清理后重跑/重试 → E_BATCH_RETAINED，归档凭证不受影响；
- 清理后导出读数证据与台账 → 来自留档凭证，与清理前逐位一致；
- 逐日台账能解释同一天多条记录为何都没算进月量。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app import errors
from app.modules.m7_export import service as export_service
from app.modules.m8_retention import ledger, service as retention_service

# 与 ledger.LEDGER_HEADER 对齐的列下标
L_LINENO, L_SEGMENT, L_DATE, L_METER, L_REGISTER, L_UTC, L_RAW, L_UNIT, L_NORM, \
    L_TYPE, L_VERDICT, L_CODE, L_REASON, L_PARTICIPATES, L_INTERVAL, L_MONTH, \
    L_NOTE = range(17)
# 与 ledger.RECEIPT_HEADER 对齐的列下标
R_RECEIVED, R_VALID, R_ESTIMATE, R_QUAR, R_BLANK, R_DISPOSED, R_DIFF, \
    R_BALANCED = 4, 5, 6, 7, 8, 9, 10, 11


def _archive_dir(settings, batch_id: str) -> Path:
    return Path(settings.db_path).resolve().parent / "archives" / batch_id


def test_archive_writes_five_evidence_files_and_checksums(settings, store, batch_id):
    result = retention_service.archive(settings, store, batch_id)
    assert result["status"] == "archived"
    assert result["reused"] is False
    directory = _archive_dir(settings, batch_id)
    names = sorted(p.name for p in directory.iterdir())
    assert names == sorted([
        retention_service.RECEIPT_NAME, retention_service.LEDGER_NAME,
        retention_service.READINGS_NAME, retention_service.QUARANTINE_NAME,
        retention_service.MANIFEST_NAME, retention_service.CHECKSUMS_NAME])
    record = store.get_archive(batch_id)
    assert record["status"] == "archived"
    assert len(record["checksum_sha256"]) == 64
    doc = json.loads((directory / retention_service.CHECKSUMS_NAME).read_text("utf-8"))
    assert doc["package_sha256"] == record["checksum_sha256"]
    # 登记的文件级哈希与磁盘实物一致
    for item in doc["files"]:
        assert item["sha256"] == retention_service.checksum.sha256_file(
            directory / item["name"])


def test_archive_is_idempotent_and_never_overwrites(settings, store, batch_id):
    first = retention_service.archive(settings, store, batch_id)
    directory = _archive_dir(settings, batch_id)
    mtimes = {p.name: p.stat().st_mtime_ns for p in directory.iterdir()}
    size_before = {p.name: p.stat().st_size for p in directory.iterdir()}
    time.sleep(0.01)
    second = retention_service.archive(settings, store, batch_id)
    assert second["reused"] is True
    assert second["checksum_sha256"] == first["checksum_sha256"]
    for name, mtime in mtimes.items():
        assert (directory / name).stat().st_mtime_ns == mtime
        assert (directory / name).stat().st_size == size_before[name]


def test_cleanup_requires_archive_first(settings, store, batch_id):
    with pytest.raises(errors.MeterbackError) as exc:
        retention_service.cleanup(settings, store, batch_id)
    assert exc.value.code == "E_ARCHIVE_NOT_FOUND"
    # 被拒绝时一行都不能删
    assert store.count_rows(batch_id)["readings"] > 0


def test_cleanup_deletes_only_readings_and_keeps_monthly_evidence(
        settings, store, batch_id):
    retention_service.archive(settings, store, batch_id)
    before = store.count_rows(batch_id)
    result = retention_service.cleanup(settings, store, batch_id)
    assert result["status"] == "cleaned"
    assert result["pruned_readings"] == before["readings"]
    after = store.count_rows(batch_id)
    assert after["readings"] == 0
    # 月量、区间、隔离、账单、表计全部保留
    for table in ("meters", "intervals", "allocations", "bills", "quarantine"):
        assert after[table] == before[table]
    record = store.get_archive(batch_id)
    assert record["status"] == "cleaned"
    assert record["cleaned_at"] is not None


def test_cleanup_retry_does_not_redelete_or_touch_evidence(settings, store, batch_id):
    retention_service.archive(settings, store, batch_id)
    retention_service.cleanup(settings, store, batch_id)
    directory = _archive_dir(settings, batch_id)
    mtimes = {p.name: p.stat().st_mtime_ns for p in directory.iterdir()}
    again = retention_service.cleanup(settings, store, batch_id)
    assert again["reused"] is True
    assert again["status"] == "cleaned"
    for name, mtime in mtimes.items():
        assert (directory / name).stat().st_mtime_ns == mtime
    assert store.count_rows(batch_id)["readings"] == 0


def test_cleanup_is_atomic_when_status_update_fails(settings, store, batch_id):
    """删除与状态登记同事务：中途失败整体回滚，不留「日值已删、状态没更新」。"""
    from app.store import Store
    retention_service.archive(settings, store, batch_id)
    readings_before = store.count_rows(batch_id)["readings"]

    def failing_tx(self, bid, stamp):
        with self.connect() as conn:
            conn.execute("DELETE FROM readings WHERE batch_id=?", (bid,))
            raise RuntimeError("simulated failure inside transaction")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(Store, "prune_readings_tx", failing_tx)
        with pytest.raises(RuntimeError):
            retention_service.cleanup(settings, store, batch_id)
    # 回滚后读数一条不少，归档状态未被推进，随后可正常重试清理
    assert store.count_rows(batch_id)["readings"] == readings_before
    assert store.get_archive(batch_id)["status"] == "archived"
    result = retention_service.cleanup(settings, store, batch_id)
    assert result["status"] == "cleaned"
    assert result["pruned_readings"] == readings_before


def test_corrupted_evidence_blocks_cleanup(settings, store, batch_id):
    retention_service.archive(settings, store, batch_id)
    readings_before = store.count_rows(batch_id)["readings"]
    target = _archive_dir(settings, batch_id) / retention_service.LEDGER_NAME
    target.write_bytes(target.read_bytes() + b"\n999,tampered\n")
    with pytest.raises(errors.MeterbackError) as exc:
        retention_service.cleanup(settings, store, batch_id)
    assert exc.value.code == "E_ARCHIVE_CORRUPT"
    # 校验失败：原始日值一行不删，归档登记仍是 archived
    assert store.count_rows(batch_id)["readings"] == readings_before
    assert store.get_archive(batch_id)["status"] == "archived"
    # verify 同样必须报错，绝不放行
    with pytest.raises(errors.MeterbackError) as verify_exc:
        retention_service.verify(settings, store, batch_id)
    assert verify_exc.value.code == "E_ARCHIVE_CORRUPT"


def test_verify_reports_ok_on_intact_archive(settings, store, batch_id):
    retention_service.archive(settings, store, batch_id)
    result = retention_service.verify(settings, store, batch_id)
    assert result["verified"] is True
    assert len(result["files"]) == 5


def test_retry_after_archive_is_rejected_and_evidence_untouched(
        settings, store, batch_id):
    from app.modules.m6_batches import service as batch_service
    retention_service.archive(settings, store, batch_id)
    directory = _archive_dir(settings, batch_id)
    snapshot = {p.name: p.read_bytes() for p in directory.iterdir()}
    with pytest.raises(errors.Conflict) as exc:
        batch_service.retry(settings, store, batch_id)
    assert exc.value.code == "E_BATCH_RETAINED"
    # 凭证字节一个不变
    for name, data in snapshot.items():
        assert (directory / name).read_bytes() == data


def test_force_run_after_cleanup_is_rejected(settings, store, batch_id):
    from app.modules.m6_batches import service as batch_service
    retention_service.archive(settings, store, batch_id)
    retention_service.cleanup(settings, store, batch_id)
    with pytest.raises(errors.Conflict) as exc:
        batch_service.run(settings, store, sample="readings_dirty.csv", mode="force")
    assert exc.value.code == "E_BATCH_RETAINED"


def test_readings_export_after_cleanup_matches_before(settings, store, batch_id):
    before_body, before_name = export_service.readings_csv(
        settings, store, batch_id)
    retention_service.archive(settings, store, batch_id)
    retention_service.cleanup(settings, store, batch_id)
    after_body, after_name = export_service.readings_csv(settings, store, batch_id)
    assert after_name == before_name
    assert after_body == before_body  # 逐位一致，来自校验过的留档凭证


def test_daily_ledger_and_receipt_export_after_cleanup(settings, store, batch_id):
    retention_service.archive(settings, store, batch_id)
    retention_service.cleanup(settings, store, batch_id)
    ledger_body, ledger_name = export_service.daily_ledger_csv(
        settings, store, batch_id)
    receipt_body, receipt_name = export_service.batch_receipt_csv(
        settings, store, batch_id)
    assert ledger_name == f"daily-ledger-{batch_id}.csv"
    assert receipt_name == f"batch-receipt-{batch_id}.csv"
    lines = ledger_body.splitlines()
    assert lines[0].lstrip("﻿").startswith("原始行号,分段,当地日期")
    # 回执差额为 0、读数账平衡
    receipt_line = receipt_body.splitlines()[1].lstrip("﻿")
    cells = receipt_line.split(",")
    assert cells[R_DIFF] == "0"         # 差额行
    assert cells[R_BALANCED] == "是"    # 读数账平衡


def test_daily_ledger_explains_quarantined_duplicates_on_same_day(
        settings, store, batch_id):
    """同一天同键两条冲突记录都被隔离：台账逐行写明原因码与「否（不参与月量）」。"""
    from app.modules.m8_retention import ledger
    retention_service.archive(settings, store, batch_id)
    rows = ledger.daily_ledger_rows(
        store.load_readings(batch_id), store.load_quarantine(batch_id),
        store.load_intervals(batch_id), store.load_allocations(batch_id))
    conflicts = [r for r in rows if r[L_CODE] == "Q_DUPLICATE_CONFLICT"]
    assert len(conflicts) >= 2
    for row in conflicts:
        assert row[L_PARTICIPATES] == "否"      # 参与月量
        assert row[L_CODE] == "Q_DUPLICATE_CONFLICT"
        assert row[L_NOTE]                      # 说明非空，解释为何没算进去


def test_daily_ledger_marks_accepted_rows_with_interval_and_month(
        settings, store, batch_id):
    from app.modules.m8_retention import ledger
    rows = ledger.daily_ledger_rows(
        store.load_readings(batch_id), store.load_quarantine(batch_id),
        store.load_intervals(batch_id), store.load_allocations(batch_id))
    participating = [r for r in rows if r[L_PARTICIPATES] == "是"]
    assert participating  # 有采信行确实进了月量
    for row in participating:
        assert row[L_INTERVAL]               # 区间号
        assert row[L_MONTH]                  # 归属月份
        assert row[L_DATE]                   # 当地日期
    # 首读行不参与月量但要写清原因，不能默默消失
    first_reads = [r for r in rows
                   if r[L_PARTICIPATES] == "否" and r[L_TYPE] != "E"
                   and r[L_CODE] == ""]
    assert any("首读" in r[L_NOTE] for r in first_reads)


def test_receipt_partition_matches_four_accounts(settings, store, batch_id):
    from app.modules.m8_retention import ledger
    totals = store.load_totals(batch_id)
    import json as _json
    reconcile = _json.loads(totals["reconcile_json"])
    batch = store.get_batch(batch_id)
    row = ledger.receipt_rows(batch, totals, reconcile)[0]
    received = int(row[R_RECEIVED])
    valid, estimate, quarantined, blank = (int(row[R_VALID]), int(row[R_ESTIMATE]),
                                           int(row[R_QUAR]), int(row[R_BLANK]))
    assert received == valid + estimate + quarantined + blank
    assert row[R_BALANCED] == "是"


def test_archive_unknown_batch_returns_not_found(settings, store):
    with pytest.raises(errors.NotFound):
        retention_service.archive(settings, store, "mb-missing")


def test_archived_file_endpoint_rejects_unsigned_name(settings, store, batch_id):
    retention_service.archive(settings, store, batch_id)
    with pytest.raises(errors.NotFound) as exc:
        retention_service.read_archived_file(
            settings, store, batch_id, "../checksums.json")
    assert exc.value.code == "E_ARCHIVE_FILE_NOT_FOUND"


def test_http_archive_and_cleanup_flow(client, settings):
    run = client.post("/api/runs", json={"sample": "readings_dirty.csv"}).json()
    bid = run["batch_id"]
    arch = client.post(f"/api/batches/{bid}/archive")
    assert arch.status_code == 200
    assert arch.json()["status"] == "archived"
    # 重复留档幂等
    again = client.post(f"/api/batches/{bid}/archive").json()
    assert again["reused"] is True
    verified = client.get(f"/api/batches/{bid}/archive/verify").json()
    assert verified["verified"] is True
    cleaned = client.post(f"/api/batches/{bid}/cleanup").json()
    assert cleaned["pruned_readings"] == run["counts"]["readings"]
    # 清理后页面与导出仍 200
    assert client.get("/report").status_code == 200
    ledger_resp = client.get("/api/export/daily-ledger.csv")
    assert ledger_resp.status_code == 200
    assert "原始行号" in ledger_resp.text
    # 清理后重试被 409 拒绝
    blocked = client.post(f"/api/batches/{bid}/retry")
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "E_BATCH_RETAINED"
    # 篡改凭证后导出读数证据应报错，不吐出失真数据
    directory = _archive_dir(settings, bid)
    target = directory / retention_service.READINGS_NAME
    target.write_bytes(target.read_bytes() + b"x")
    bad = client.get("/api/export/readings.csv")
    assert bad.status_code == 409
    assert bad.json()["error"] == "E_ARCHIVE_CORRUPT"


def test_archive_dir_name_is_batch_scoped(settings, store, batch_id):
    retention_service.archive(settings, store, batch_id)
    root = Path(settings.db_path).resolve().parent / "archives"
    assert (root / batch_id).is_dir()
