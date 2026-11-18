"""留档清理编排：先归档、再校验、后清理，凭证只写一次、重试不覆盖。

流程分两步，各自幂等且互不破坏：
1. archive：把批次的逐日裁决台账、批次回执、读数证据、隔离证据、运行清单
   写到 data/archives/<batch_id>/，逐文件算 sha256 并登记整包指纹。
   目录或登记已存在时直接返回既有凭证，绝不重写文件（write-once）。
2. cleanup：重新读取已落盘凭证并核对 sha256，全部一致才删 readings 原始日值；
   月量、区间、隔离、清单、归档登记保留，月报导出回退到归档台账，仍可独立对账。
   清理可安全重试：已清理批次再调一次只回显结果，不重复删除、不动凭证。
"""
from __future__ import annotations

import json
from pathlib import Path

from ... import errors
from ..m7_export import csv_out, evidence, manifest_out
from . import checksum, ledger

RECEIPT_NAME = "batch-receipt.csv"
LEDGER_NAME = "daily-ledger.csv"
READINGS_NAME = "readings-evidence.csv"
QUARANTINE_NAME = "quarantine-evidence.csv"
MANIFEST_NAME = "run-manifest.json"
CHECKSUMS_NAME = "checksums.json"


class ArchiveError(errors.MeterbackError):
    status_code = 409
    code = "E_ARCHIVE_CONFLICT"


def archive_root(settings) -> Path:
    return Path(settings.db_path).resolve().parent / "archives"


def _require_done_batch(store, batch_id: str) -> dict:
    batch = store.get_batch(batch_id)
    if batch is None:
        raise errors.NotFound(f"批次 {batch_id} 不存在", code="E_BATCH_NOT_FOUND")
    if batch["status"] != "done":
        raise ArchiveError(
            f"批次 {batch_id} 状态为 {batch['status']}，只有完成批次才能留档",
            code="E_ARCHIVE_NOT_DONE")
    return batch


def _render_files(settings, store, batch: dict) -> dict[str, bytes]:
    ident = batch["batch_id"]
    totals = store.load_totals(ident)
    reconcile = json.loads(totals.get("reconcile_json") or "null")
    manifest_payload = manifest_out.build(
        store, batch, store.load_manifest(ident))
    bodies = {
        RECEIPT_NAME: csv_out.render(
            ledger.RECEIPT_HEADER,
            ledger.receipt_rows(batch, totals, reconcile)),
        LEDGER_NAME: csv_out.render(
            ledger.LEDGER_HEADER,
            ledger.daily_ledger_rows(
                store.load_readings(ident), store.load_quarantine(ident),
                store.load_intervals(ident), store.load_allocations(ident))),
        READINGS_NAME: csv_out.render(
            evidence.READING_HEADER,
            evidence.reading_rows(store.load_readings(ident))),
        QUARANTINE_NAME: csv_out.render(
            evidence.QUARANTINE_HEADER,
            evidence.quarantine_rows(store.load_quarantine(ident))),
        MANIFEST_NAME: manifest_out.render_json(manifest_payload),
    }
    return {name: body.encode("utf-8") for name, body in bodies.items()}


def _verify_files(directory: Path, expected: dict[str, str]) -> None:
    """逐文件比对磁盘上的实际 sha256；任何缺失或被改都判失败，不允许清理。"""
    problems = []
    for name, want in expected.items():
        path = directory / name
        if not path.is_file():
            problems.append(f"{name} 缺失")
            continue
        actual = checksum.sha256_file(path)
        if actual != want:
            problems.append(f"{name} 校验和不一致")
    if problems:
        raise ArchiveError("归档凭证校验失败：" + "；".join(problems),
                           code="E_ARCHIVE_CORRUPT")


def archive(settings, store, batch_id: str) -> dict:
    """生成或取回留档凭证。已归档时原样返回，不覆盖任何文件。"""
    batch = _require_done_batch(store, batch_id)
    existing = store.get_archive(batch_id)
    directory = archive_root(settings) / batch_id
    if existing is not None:
        # 凭证已登记：以磁盘实物为准复核一次，再回显；不重写、不重新登记
        _verify_files(directory, {f["name"]: f["sha256"] for f in existing["files"]})
        return {"status": existing["status"], "batch_id": batch_id,
                "archive_dir": existing["archive_dir"], "reused": True,
                "files": existing["files"],
                "checksum_sha256": existing["checksum_sha256"]}

    if directory.exists() and any(directory.iterdir()):
        # 目录里有东西但库里没登记：不是本服务写出的完整凭证，拒绝覆盖
        raise ArchiveError(
            f"留档目录 {directory} 已存在且非空，缺少登记记录，拒绝覆盖",
            code="E_ARCHIVE_DIR_EXISTS")

    files_bytes = _render_files(settings, store, batch)
    file_checksums = {name: checksum.sha256_bytes(data)
                      for name, data in files_bytes.items()}
    fingerprint = checksum.package_fingerprint(file_checksums)
    manifest_sha = file_checksums[MANIFEST_NAME]

    directory.mkdir(parents=True, exist_ok=True)
    written: list[dict] = []
    for name, data in files_bytes.items():
        target = directory / name
        # 最后一道防线：open 用 x 模式，文件已存在直接报错，绝不覆盖
        with open(target, "xb") as fh:
            fh.write(data)
        written.append({"name": name, "sha256": file_checksums[name],
                        "bytes": len(data)})
    checksums_doc = {
        "batch_id": batch_id, "algorithm": "sha256",
        "package_sha256": fingerprint,
        "files": sorted(written, key=lambda item: item["name"]),
    }
    with open(directory / CHECKSUMS_NAME, "x", encoding="utf-8") as fh:
        fh.write(json.dumps(checksums_doc, ensure_ascii=False, indent=2) + "\n")

    from ..m6_batches import history
    stamp = history.iso(history.now_utc())
    record = {
        "batch_id": batch_id, "archive_dir": str(directory), "status": "archived",
        "created_at": stamp, "archived_at": stamp, "cleaned_at": None,
        "files": written, "checksum_sha256": fingerprint,
        "manifest_sha256": manifest_sha,
        "entry_count": len(written), "pruned_readings": 0, "note": "",
    }
    try:
        store.insert_archive(record)
    except Exception:
        # 登记失败也不留半成品：已写文件属于本次未登记凭证，回滚目录内容
        for child in directory.iterdir():
            child.unlink()
        raise
    return {"status": "archived", "batch_id": batch_id,
            "archive_dir": str(directory), "reused": False, "files": written,
            "checksum_sha256": fingerprint}


def verify(settings, store, batch_id: str) -> dict:
    """对外的凭证校验：读磁盘实物比对登记哈希，清理前后都能复查。"""
    record = store.get_archive(batch_id)
    if record is None:
        raise errors.NotFound(f"批次 {batch_id} 还没有留档凭证",
                              code="E_ARCHIVE_NOT_FOUND")
    directory = Path(record["archive_dir"])
    _verify_files(directory, {f["name"]: f["sha256"] for f in record["files"]})
    return {"batch_id": batch_id, "status": record["status"],
            "verified": True, "files": record["files"],
            "checksum_sha256": record["checksum_sha256"]}


def cleanup(settings, store, batch_id: str) -> dict:
    """校验通过后清理原始日值。已清理则直接回显，重试不重复删除、不覆盖凭证。"""
    record = store.get_archive(batch_id)
    if record is None:
        raise ArchiveError(f"批次 {batch_id} 尚未留档，不能清理原始日值",
                           code="E_ARCHIVE_NOT_FOUND")
    directory = Path(record["archive_dir"])
    _verify_files(directory, {f["name"]: f["sha256"] for f in record["files"]})

    if record["status"] == "cleaned":
        return {"status": "cleaned", "batch_id": batch_id, "reused": True,
                "pruned_readings": record["pruned_readings"],
                "checksum_sha256": record["checksum_sha256"]}

    before = store.count_rows(batch_id)
    from ..m6_batches import history
    stamp = history.iso(history.now_utc())
    # 删除与状态更新在同一个事务里：失败整体回滚，不留半清理
    pruned = store.prune_readings_tx(batch_id, stamp)
    if pruned != before["readings"]:
        # 计数对不上说明库被外部动过，事务已回滚，绝不留「半清理」状态
        raise ArchiveError(
            f"清理行数 {pruned} 与登记读数 {before['readings']} 不一致，已回滚",
            code="E_ARCHIVE_CLEAN_MISMATCH")
    return {"status": "cleaned", "batch_id": batch_id, "reused": False,
            "pruned_readings": pruned,
            "checksum_sha256": record["checksum_sha256"]}


def read_archived_file(settings, store, batch_id: str, name: str) -> tuple[bytes, str]:
    """清理后导出回退用：只允许读取已登记的凭证文件，按登记哈希复核后返回。"""
    record = store.get_archive(batch_id)
    if record is None:
        raise errors.NotFound(f"批次 {batch_id} 还没有留档凭证",
                              code="E_ARCHIVE_NOT_FOUND")
    allowed = {f["name"]: f["sha256"] for f in record["files"]}
    if name not in allowed:
        raise errors.NotFound(f"凭证 {name} 不在留档清单内",
                              code="E_ARCHIVE_FILE_NOT_FOUND")
    path = Path(record["archive_dir"]) / name
    # 先看文件在不在，再校验哈希：缺失与被改是两种不同的失败，都不允许吐出数据
    if not path.is_file():
        raise ArchiveError(f"凭证文件缺失：{name}", code="E_ARCHIVE_CORRUPT")
    if checksum.sha256_file(path) != allowed[name]:
        raise ArchiveError(f"凭证 {name} 校验和不一致，拒绝导出",
                           code="E_ARCHIVE_CORRUPT")
    return path.read_bytes(), name
