"""Small shared helpers: retry with backoff, hashing, filename safety, run lock."""
from __future__ import annotations

import hashlib
import logging
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Tuple, Type, TypeVar

T = TypeVar("T")
log = logging.getLogger("exch.utils")


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int,
    retry_on: Tuple[Type[BaseException], ...],
    what: str,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    logger: logging.Logger = log,
) -> T:
    """Call fn() with exponential backoff + jitter. Re-raises the last error."""
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:  # type: ignore[misc]
            last = exc
            if attempt >= attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1))) + random.uniform(0, 1)
            logger.warning("%s failed (attempt %d/%d): %s — retrying in %.1fs",
                           what, attempt, attempts, exc, delay)
            time.sleep(delay)
    assert last is not None
    logger.error("%s failed after %d attempts: %s", what, attempts, last)
    raise last


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def who_is_locking(path: Path) -> str:
    """Names of the programs that have `path` open, e.g. "MsMpEng.exe (pid 4120)".

    Uses the Windows Restart Manager. Returns "" when unknown or not on Windows;
    never raises (it is only used to make "Access is denied" errors explainable)."""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class RM_UNIQUE_PROCESS(ctypes.Structure):
            _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]

        class RM_PROCESS_INFO(ctypes.Structure):
            _fields_ = [("Process", RM_UNIQUE_PROCESS), ("strAppName", wintypes.WCHAR * 256),
                        ("strServiceShortName", wintypes.WCHAR * 64), ("ApplicationType", ctypes.c_int),
                        ("AppStatus", wintypes.ULONG), ("TSSessionId", wintypes.DWORD),
                        ("bRestartable", wintypes.BOOL)]

        rm = ctypes.WinDLL("rstrtmgr")
        session = wintypes.DWORD()
        key = ctypes.create_unicode_buffer(33)
        if rm.RmStartSession(ctypes.byref(session), 0, key):
            return ""
        try:
            files = (wintypes.LPCWSTR * 1)(str(Path(path).resolve()))
            if rm.RmRegisterResources(session, 1, files, 0, None, 0, None):
                return ""
            needed, count, reasons = wintypes.UINT(), wintypes.UINT(10), wintypes.DWORD()
            infos = (RM_PROCESS_INFO * 10)()
            if rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), infos, ctypes.byref(reasons)):
                return ""
            names = []
            for info in infos[: count.value]:
                pid = info.Process.dwProcessId
                exe = ""
                try:  # the exe name is clearer than the app's display name
                    import psutil
                    exe = psutil.Process(pid).name()
                except Exception:
                    pass
                names.append(f"{exe or info.strAppName} (pid {pid})")
            return ", ".join(names)
        finally:
            rm.RmEndSession(session)
    except Exception:
        return ""


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str, max_len: int = 120) -> str:
    """Make a string safe to use as a Windows file/folder name."""
    cleaned = _BAD_CHARS.sub("_", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "unnamed"
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if cleaned.split(".")[0].upper() in reserved:
        cleaned = "_" + cleaned
    if len(cleaned) > max_len:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and len(ext) <= 6:
            cleaned = stem[: max_len - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:max_len]
    return cleaned


class RunLock:
    """Prevents two pipeline runs from overlapping (e.g. manual run + scheduled run)."""

    def __init__(self, path: Path, stale_after_hours: float = 6.0):
        self.path = path
        self.stale_after = stale_after_hours * 3600
        self.acquired = False

    def __enter__(self) -> "RunLock":
        if self.path.exists():
            age = time.time() - self.path.stat().st_mtime
            if age < self.stale_after:
                raise RuntimeError(
                    f"Another run appears to be in progress (lock file {self.path}, "
                    f"age {age/60:.0f} min). Delete the lock file if that run crashed."
                )
            log.warning("Removing stale lock file (age %.1f h)", age / 3600)
            self.path.unlink(missing_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as fh:
            fh.write(f"pid={os.getpid()} started={datetime.now().isoformat()}\n")
        self.acquired = True
        return self

    def __exit__(self, *exc) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
