"""Read-only drive listing and cache measurement."""

from __future__ import annotations

import ctypes
import os
import shutil
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from cleaner.safety import deletion_allowed, extended_path, is_reparse_point, is_within, normalize

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_get_logical_drives = _kernel32.GetLogicalDrives
_get_logical_drives.restype = ctypes.c_uint32
_get_drive_type = _kernel32.GetDriveTypeW
_get_drive_type.argtypes = [ctypes.c_wchar_p]
_get_drive_type.restype = ctypes.c_uint32

DRIVE_TYPES = {
    0: "Unknown",
    1: "No root",
    2: "Removable",
    3: "Fixed",
    4: "Network",
    5: "CD-ROM",
    6: "RAM disk",
}
# Fixed disks are always checked. Removable, network, and RAM disks are checked
# only when Windows can read them quickly, so an empty card reader or a dead
# network mapping cannot stall the scan.
SCANNABLE_KINDS = {"Removable", "Fixed", "Network", "RAM disk"}
READY_TIMEOUT_SECONDS = 1.5


@dataclass
class DriveInfo:
    root: Path
    kind: str
    ready: bool
    free_bytes: int | None = None
    total_bytes: int | None = None
    note: str = ""


@dataclass
class FileEntry:
    path: Path
    size: int
    root: Path
    category_id: str
    min_age_seconds: int | None
    allow_program_files_cache: bool


@dataclass
class CategoryResult:
    category_id: str
    label: str
    description: str
    entries: list[FileEntry] = field(default_factory=list)
    skipped_recent: int = 0
    skipped_unsafe: int = 0
    unreadable: int = 0
    roots_found: int = 0

    @property
    def size(self) -> int:
        return sum(entry.size for entry in self.entries)


@dataclass
class ScanReport:
    drives: list[DriveInfo]
    categories: list[CategoryResult]
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False


def _drive_type(root: str) -> str:
    code = int(_get_drive_type(root))
    return DRIVE_TYPES.get(code, "Unknown")


def _space(root: Path) -> tuple[int, int] | None:
    try:
        usage = shutil.disk_usage(root)
    except OSError:
        return None
    return usage.free, usage.total


def _call_with_timeout(fn, timeout: float, default):
    box: list = []

    def run() -> None:
        try:
            box.append(fn())
        except Exception:
            box.append(None)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if not box:
        return default
    return box[0]


def list_drives() -> list[DriveInfo]:
    bitmask = int(_get_logical_drives())
    drives: list[DriveInfo] = []
    for index in range(26):
        if not bitmask & (1 << index):
            continue
        root = Path(f"{chr(65 + index)}:\\")
        kind = _drive_type(str(root))
        info = DriveInfo(root=root, kind=kind, ready=False)
        if kind not in SCANNABLE_KINDS:
            info.note = "not scanned"
            drives.append(info)
            continue
        # Local fixed disks respond immediately. Other types get a short budget
        # so an offline share cannot hold the scan open.
        timeout = 8.0 if kind == "Fixed" else READY_TIMEOUT_SECONDS
        space = _call_with_timeout(lambda r=root: _space(r), timeout, None)
        if space is None:
            info.note = "not ready"
            drives.append(info)
            continue
        info.ready = True
        info.free_bytes, info.total_bytes = space
        drives.append(info)
    return drives


def refresh_free_space(drives: list[DriveInfo]) -> None:
    for drive in drives:
        if not drive.ready:
            continue
        space = _space(drive.root)
        if space is not None:
            drive.free_bytes, drive.total_bytes = space


def _iter_files(root: Path, *, recursive: bool, name_globs: tuple[str, ...] | None, errors: list | None = None):
    def record(exc: OSError) -> None:
        if errors is not None:
            errors.append(exc)

    if name_globs is not None:
        for pattern in name_globs:
            try:
                matches = list(root.glob(pattern))
            except OSError as exc:
                record(exc)
                continue
            for path in matches:
                if path.is_file() and not is_reparse_point(path):
                    yield path
        return

    if not recursive:
        try:
            children = list(root.iterdir())
        except OSError as exc:
            record(exc)
            return
        for path in children:
            if path.is_file() and not is_reparse_point(path):
                yield path
        return

    walk_root = extended_path(root)
    try:
        walker = os.walk(walk_root, followlinks=False, onerror=record)
    except OSError as exc:
        record(exc)
        return
    for dirpath, dirnames, filenames in walker:
        kept: list[str] = []
        for name in dirnames:
            candidate = Path(dirpath) / name
            if is_reparse_point(candidate):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            candidate = Path(dirpath) / name
            if is_reparse_point(candidate):
                continue
            yield candidate


def _stat_file(path: Path):
    try:
        return os.stat(extended_path(path), follow_symlinks=False)
    except OSError:
        try:
            return os.stat(path, follow_symlinks=False)
        except OSError:
            return None


def format_bytes(num: int) -> str:
    value = float(max(num, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def scan(categories, drives: list[DriveInfo], *, progress=None, cancel: threading.Event | None = None, now: float | None = None) -> ScanReport:
    """Measure whitelist categories. Does not delete anything."""
    clock = time.time() if now is None else now
    report = ScanReport(drives=drives, categories=[])
    seen: set[str] = set()

    for category in categories:
        if cancel is not None and cancel.is_set():
            report.cancelled = True
            break
        result = CategoryResult(
            category_id=category.category_id,
            label=category.label,
            description=category.description,
            roots_found=len(category.roots),
        )
        if progress is not None:
            progress(f"Scanning {category.label}…")
        for root in category.roots:
            if cancel is not None and cancel.is_set():
                report.cancelled = True
                break
            try:
                resolved_root = normalize(root)
            except OSError:
                result.unreadable += 1
                continue
            listing_errors: list[OSError] = []
            for path in _iter_files(
                root,
                recursive=category.recursive,
                name_globs=category.name_globs,
                errors=listing_errors,
            ):
                if cancel is not None and cancel.is_set():
                    report.cancelled = True
                    break
                try:
                    resolved = normalize(path)
                except OSError:
                    result.unreadable += 1
                    continue
                if not is_within(resolved, resolved_root):
                    result.skipped_unsafe += 1
                    continue
                key = os.path.normcase(str(resolved))
                if key in seen:
                    continue
                info = _stat_file(path)
                if info is None or not stat.S_ISREG(info.st_mode):
                    result.unreadable += 1
                    continue
                allowed, reason = deletion_allowed(
                    path,
                    root,
                    allow_program_files_cache=category.allow_program_files_cache,
                    min_age_seconds=category.min_age_seconds,
                    now=clock,
                )
                if not allowed:
                    if reason == "changed in the last hour":
                        result.skipped_recent += 1
                    else:
                        result.skipped_unsafe += 1
                    continue
                seen.add(key)
                result.entries.append(
                    FileEntry(
                        path=resolved,
                        size=info.st_size,
                        root=resolved_root,
                        category_id=category.category_id,
                        min_age_seconds=category.min_age_seconds,
                        allow_program_files_cache=category.allow_program_files_cache,
                    )
                )
            result.unreadable += len(listing_errors)
            if report.cancelled:
                break
        result.entries.sort(key=lambda entry: entry.size, reverse=True)
        report.categories.append(result)
    return report
