"""P0-2 事务写入（v2：编码自适应 + Windows 重试）。

保证 AGENTS.md 这一类「被多个进程/多次调用反复改写」的小文件：
  1. 并发安全：目录级文件锁（跨平台：Unix fcntl / Windows 用 O_EXCL 锁文件）。
  2. 原子性：先写临时文件 -> fsync -> os.replace 原地替换，读方永远看到完整文件。
  3. Fail-Closed：写前可校验「我读到的那份」是否仍是磁盘上的当前版本；
     若外部已改动（hash 不匹配），直接拒绝写入而非基于过期内容覆盖。
  4. 编码自适应：读取自动识别 UTF-8（含 BOM）与 GBK（中文 Windows 常见旧文件）；
     写入统一为 UTF-8，保证 hash 校验与内容一致。
  5. Windows 重试：目标文件被其它进程短暂打开时 os.replace 会抛
     PermissionError，小幅退避重试后再放弃。

零三方依赖：锁用 os.open(O_CREAT|O_EXCL) 的锁文件实现，并带「过期锁回收」。
"""

import hashlib
import os
import time
from pathlib import Path


class ConcurrentModification(Exception):
    """写前 hash 校验失败：磁盘内容已被外部修改，拒绝覆盖以保持一致性。"""


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def acquire_lock(path: Path, timeout: float = 10.0, stale_after: float = 60.0) -> Path:
    """获取锁文件；超时抛 TimeoutError。stale_after 秒前的孤儿锁会被回收。

    stale_after 默认 60s 对超大文件（>1MB 几乎不可能出现在 AGENTS.md 场景）
    不足时可由调用方按文件大小放大，见 stale_after_for()。
    """
    lp = _lock_path(path)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
            try:
                os.write(fd, str(os.getpid()).encode("utf-8"))
            finally:
                os.close(fd)
            return lp
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lp) > stale_after:
                    os.remove(lp)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise TimeoutError(f"无法在 {timeout}s 内获取锁：{lp}")
            time.sleep(0.1)


def stale_after_for(path: Path, base: float = 60.0) -> float:
    """按文件大小放大锁的过期阈值，防止大文件写入被误回收。"""
    try:
        size = path.stat().st_size
    except OSError:
        return base
    # 每 64KB 多给 15s，上限 10 分钟
    return min(base + (size // 65536) * 15.0, 600.0)


def release_lock(lp: Path) -> None:
    try:
        os.remove(lp)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_newlines(text: str) -> str:
    """统一为 \\n：消除 Windows 平台 \\r\\n 与读写路径不一致导致的 hash 漂移。"""
    return text.replace("\r\n", "\n")


def read_text(path: Path) -> str:
    """读取文本：优先 UTF-8（容忍 BOM），失败回退 GBK（中文 Windows 旧文件）。

    统一从这里读文件，保证与 atomic_write 的 hash 校验基准一致：
    无论磁盘原编码/换行如何，校验与写入都以「UTF-8 + LF」的归一化文本为准。
    """
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "gbk"):
        try:
            return _normalize_newlines(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("utf-8", raw, 0, len(raw) or 1, "无法识别的文本编码")


def _replace_with_retry(tmp: Path, path: Path, attempts: int = 3, delay: float = 0.25):
    """os.replace 在 Windows 上可能因目标被读打开而 PermissionError，退避重试。"""
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))
    # 理论不可达：attempts >= 1 时最后循环必然 return 或 raise
    os.replace(tmp, path)


def atomic_write(path: Path, text: str, expected_hash: str | None = None) -> None:
    """事务性写文件。

    expected_hash: 若提供，写前比对磁盘当前内容 hash；不匹配则抛
    ConcurrentModification（Fail-Closed），绝不基于过期内容写回。
    写入统一为 UTF-8（无 BOM）。
    """
    path = Path(path)
    lp = acquire_lock(path, stale_after=stale_after_for(path))
    try:
        current = read_text(path) if path.exists() else ""
        if expected_hash is not None and sha256_text(current) != expected_hash:
            raise ConcurrentModification(
                "AGENTS.md 已被外部修改（hash 不匹配），拒绝基于过期内容写回。"
            )
        tmp = path.with_name(path.name + ".tmp")
        # O_BINARY 关键：Windows 上 os.open 默认文本模式会把 \n 写成 \r\n，
        # 造成读回（read_bytes）与写入内容不一致、hash 永远不匹配。
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
        fd = os.open(str(tmp), flags, 0o644)
        try:
            os.write(fd, _normalize_newlines(text).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        _replace_with_retry(tmp, path)  # 同文件系统内原子替换（带 Windows 重试）
    finally:
        release_lock(lp)
