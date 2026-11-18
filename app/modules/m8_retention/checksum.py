"""归档校验和：凭证文件按固定顺序逐个 sha256，再汇总成整包指纹。

两层校验，清理后仍能发现凭证被改：
- 文件级：每个 CSV/JSON 的 sha256 写进 checksums 与归档登记；
- 包级：把「文件名 + 文件 sha256」按文件名排序后再哈希一次，得到整包指纹。
校验只读取已落盘的字节，不复算内容，避免「用同一套可能出错的逻辑自证」。
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def package_fingerprint(file_checksums: dict[str, str]) -> str:
    """整包指纹：文件名排序后拼接各自哈希，保证与写入顺序无关、可复算。"""
    h = hashlib.sha256()
    for name in sorted(file_checksums):
        h.update(name.encode("utf-8"))
        h.update(b"\x00")
        h.update(file_checksums[name].encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()
