"""Delete files that have already passed the safety check, then check again."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cleaner.safety import deletion_allowed, extended_path, is_reparse_point, is_within, normalize


@dataclass
class CleanResult:
    deleted_count: int = 0
    deleted_bytes: int = 0
    deleted: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    removed_dirs: int = 0


def _unlink(path: Path) -> None:
    target = extended_path(path)
    try:
        os.remove(target)
    except PermissionError as exc:
        # Clear the read-only bit only. A sharing violation means an app still
        # has the file open, and changing its attributes would not free it.
        if getattr(exc, "winerror", None) not in {None, 5}:
            raise
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        os.remove(target)


def _in_use(exc: OSError) -> bool:
    # 5 access denied, 32/33 sharing violation. These are files an app still has open.
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}


def _remove_empty_parents(deleted_files: list[tuple[Path, Path]], result: CleanResult) -> None:
    """Remove folders that became empty because we deleted cache files.

    Only parents of files we actually removed are considered, and the cache
    root itself is never removed. Unrelated empty folders stay.
    """
    parents: set[tuple[str, str]] = set()
    for file_path, root in deleted_files:
        parent = file_path.parent
        while is_within(parent, root) and os.path.normcase(str(parent)) != os.path.normcase(str(root)):
            parents.add((os.path.normcase(str(parent)), str(parent)))
            parent = parent.parent
    ordered = sorted(parents, key=lambda item: len(item[0]), reverse=True)
    for _, text in ordered:
        directory = Path(text)
        if is_reparse_point(directory):
            continue
        try:
            os.rmdir(extended_path(directory))
        except OSError:
            continue
        result.removed_dirs += 1


def clean(entries, *, now: float) -> CleanResult:
    result = CleanResult()
    deleted_files: list[tuple[Path, Path]] = []
    for entry in entries:
        try:
            resolved_file = normalize(entry.path)
            resolved_root = normalize(entry.root)
        except OSError as exc:
            result.skipped.append((str(entry.path), f"could not resolve ({exc})"))
            continue
        allowed, reason = deletion_allowed(
            resolved_file,
            resolved_root,
            allow_program_files_cache=entry.allow_program_files_cache,
            min_age_seconds=entry.min_age_seconds,
            now=now,
        )
        if not allowed:
            result.skipped.append((str(resolved_file), reason))
            continue
        try:
            _unlink(resolved_file)
        except FileNotFoundError:
            result.skipped.append((str(resolved_file), "already gone"))
            continue
        except OSError as exc:
            if _in_use(exc):
                result.skipped.append((str(resolved_file), "in use"))
            else:
                result.errors.append((str(resolved_file), str(exc)))
            continue
        result.deleted_count += 1
        result.deleted_bytes += entry.size
        result.deleted.append(str(resolved_file))
        deleted_files.append((resolved_file, resolved_root))
    _remove_empty_parents(deleted_files, result)
    return result


def write_log(path: Path, result: CleanResult) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "PC Cleaner log",
        f"Time: {stamp}",
        f"Deleted: {result.deleted_count} files ({result.deleted_bytes} bytes)",
        f"Empty folders removed: {result.removed_dirs}",
        f"Skipped: {len(result.skipped)}",
        f"Errors: {len(result.errors)}",
        "",
        "DELETED",
    ]
    lines.extend(result.deleted or ["(none)"])
    lines.append("")
    lines.append("SKIPPED")
    if result.skipped:
        lines.extend(f"{file_path}\t{reason}" for file_path, reason in result.skipped)
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("ERRORS")
    if result.errors:
        lines.extend(f"{file_path}\t{reason}" for file_path, reason in result.errors)
    else:
        lines.append("(none)")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
