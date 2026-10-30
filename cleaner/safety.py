"""Last-line checks before any cache file is deleted."""

from __future__ import annotations

import ctypes
import os
import stat
from pathlib import Path

# Folder names that mean "user files" or "Windows itself". A cache target that
# resolves into one of these is refused, even if discovery was wrong.
PERSONAL_OR_SYSTEM_DIRS = frozenset(
    {
        "desktop",
        "documents",
        "downloads",
        "pictures",
        "videos",
        "music",
        "favorites",
        "contacts",
        "onedrive",
        "my documents",
        "my music",
        "my pictures",
        "my videos",
        "system32",
        "syswow64",
        "winsxs",
        "steamapps",
        "cookies",
        "local storage",
        "session storage",
        "indexeddb",
    }
)

# Profile files that must survive even if they sit inside an allowed folder.
FORBIDDEN_FILE_NAMES = frozenset(
    {
        "cookies",
        "cookies-journal",
        "cookies.sqlite",
        "cookies.sqlite-wal",
        "cookies.sqlite-shm",
        "login data",
        "login data-journal",
        "login data for account",
        "history",
        "history-journal",
        "bookmarks",
        "bookmarks.bak",
        "web data",
        "web data-journal",
        "preferences",
        "secure preferences",
        "places.sqlite",
        "places.sqlite-wal",
        "places.sqlite-shm",
        "logins.json",
        "key4.db",
        "cert9.db",
        "favicons.sqlite",
        "permissions.sqlite",
        "pagefile.sys",
        "hiberfil.sys",
        "swapfile.sys",
        "ntuser.dat",
        "ntuser.dat.log",
        "ntuser.ini",
    }
)

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_get_file_attributes = _kernel32.GetFileAttributesW
_get_file_attributes.argtypes = [ctypes.c_wchar_p]
_get_file_attributes.restype = ctypes.c_uint32


def normalize(path: Path) -> Path:
    """Absolute path without the ``\\\\?\\`` prefix, symlinks resolved."""
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return Path(text).resolve()


def extended_path(path: Path) -> str:
    """Win32 path that can address files longer than MAX_PATH."""
    resolved = normalize(path)
    text = str(resolved)
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _file_attributes(path: str) -> int:
    return int(_get_file_attributes(path))


def is_reparse_point(path: Path) -> bool:
    attributes = _file_attributes(str(path))
    if attributes == INVALID_FILE_ATTRIBUTES:
        try:
            attributes = _file_attributes(extended_path(path))
        except OSError:
            return False
    # A failed lookup is not a link. Treating the failure code as flags would
    # mark every missing path as a reparse point.
    if attributes == INVALID_FILE_ATTRIBUTES:
        return False
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def is_within(child: Path, parent: Path) -> bool:
    """True when ``child`` is ``parent`` or a descendant. Case-insensitive."""
    child_norm = os.path.normcase(os.path.normpath(str(child)))
    parent_norm = os.path.normcase(os.path.normpath(str(parent)))
    if child_norm == parent_norm:
        return True
    return child_norm.startswith(parent_norm + os.sep)


def _parts(path: Path) -> tuple[str, ...]:
    return tuple(part.casefold() for part in path.parts)


def touches_personal_or_system(path: Path) -> bool:
    for part in _parts(path):
        if part in PERSONAL_OR_SYSTEM_DIRS or part.startswith("onedrive"):
            return True
    return False


def is_under_program_files(path: Path) -> bool:
    parts = _parts(path)
    return "program files" in parts or "program files (x86)" in parts


def is_steam_htmlcache(path: Path) -> bool:
    """True only for ``...\\Steam\\htmlcache`` and nothing under steamapps."""
    parts = _parts(path)
    if "steamapps" in parts:
        return False
    return len(parts) >= 2 and parts[-1] == "htmlcache" and parts[-2] == "steam"


def root_is_allowed(root: Path, *, allow_program_files_cache: bool) -> tuple[bool, str]:
    try:
        resolved = normalize(root)
    except OSError as exc:
        return False, f"could not resolve ({exc})"
    if is_reparse_point(root) or is_reparse_point(resolved):
        return False, "cache location is a link"
    if touches_personal_or_system(resolved):
        return False, "inside a personal or system folder"
    if is_under_program_files(resolved):
        if allow_program_files_cache and is_steam_htmlcache(resolved):
            return True, ""
        return False, "inside Program Files"
    return True, ""


def deletion_allowed(
    file_path: Path,
    root: Path,
    *,
    allow_program_files_cache: bool,
    min_age_seconds: int | None,
    now: float,
) -> tuple[bool, str]:
    """Return whether this one file may be deleted, and why not."""
    try:
        resolved_root = normalize(root)
        resolved_file = normalize(file_path)
    except OSError as exc:
        return False, f"could not resolve ({exc})"

    if not is_within(resolved_file, resolved_root):
        return False, "outside the cache folder"
    if os.path.normcase(str(resolved_file)) == os.path.normcase(str(resolved_root)):
        return False, "refusing to delete the cache folder itself"
    # The leaf is a file name. "Cookies" is a protected file, not a protected folder,
    # and is rejected by the file-name list below.
    if touches_personal_or_system(resolved_file.parent) or touches_personal_or_system(resolved_root):
        return False, "inside a personal or system folder"
    if is_under_program_files(resolved_file) or is_under_program_files(resolved_root):
        if not (allow_program_files_cache and is_steam_htmlcache(resolved_root)):
            return False, "inside Program Files"
    if is_reparse_point(file_path) or is_reparse_point(resolved_file):
        return False, "skipping link"

    name = resolved_file.name.casefold()
    if name in FORBIDDEN_FILE_NAMES:
        return False, "profile or system file"

    try:
        st = os.stat(extended_path(resolved_file), follow_symlinks=False)
    except OSError as exc:
        return False, f"could not read ({exc})"
    if not stat.S_ISREG(st.st_mode):
        return False, "not a file"
    if min_age_seconds is not None and (now - st.st_mtime) < min_age_seconds:
        return False, "changed in the last hour"
    return True, ""
