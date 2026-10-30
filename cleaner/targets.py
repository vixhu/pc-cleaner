"""Whitelist of cache locations for the current Windows user.

Discovery never searches a drive for folders named Cache. It only checks these
known paths, on each ready drive, for the account that launched the app.
"""

from __future__ import annotations

import os
import re
import winreg
from dataclasses import dataclass
from pathlib import Path

from cleaner.safety import is_reparse_point, normalize, root_is_allowed

TEMP_MIN_AGE_SECONDS = 60 * 60
_PROFILE_DIR = re.compile(r"^(Default|Guest Profile|Profile \d+)$")
_CHROMIUM_CACHE_DIRS = ("Cache", "Code Cache", "GPUCache", "ShaderCache")
_CHROMIUM_SHARED_CACHE_DIRS = ("ShaderCache", "GrShaderCache", "GraphiteDawnCache")


@dataclass(frozen=True)
class CategoryPlan:
    category_id: str
    label: str
    description: str
    roots: tuple[Path, ...]
    min_age_seconds: int | None = None
    name_globs: tuple[str, ...] | None = None
    recursive: bool = True
    allow_program_files_cache: bool = False


def _dedupe(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            key = os.path.normcase(str(normalize(path)))
        except OSError:
            key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _local_appdata_dirs(drive_roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        found.append(Path(local))
    username = os.environ.get("USERNAME")
    if username:
        for drive in drive_roots:
            found.append(drive / "Users" / username / "AppData" / "Local")
    return _dedupe(found)


def _roaming_dirs(drive_roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    roaming = os.environ.get("APPDATA")
    if roaming:
        found.append(Path(roaming))
    username = os.environ.get("USERNAME")
    if username:
        for drive in drive_roots:
            found.append(drive / "Users" / username / "AppData" / "Roaming")
    return _dedupe(found)


def _existing_dirs(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        try:
            if path.is_dir():
                found.append(path)
        except OSError:
            continue
    return _dedupe(found)


def _user_temp_candidates(drive_roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    for key in ("TEMP", "TMP"):
        value = os.environ.get(key)
        if value:
            found.append(Path(value))
    for local in _local_appdata_dirs(drive_roots):
        found.append(local / "Temp")
    return found


def _windows_temp_candidates(drive_roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    windir = os.environ.get("WINDIR")
    if windir:
        found.append(Path(windir) / "Temp")
    for drive in drive_roots:
        found.append(drive / "Windows" / "Temp")
    return found


def _chromium_caches(user_data: Path) -> list[Path]:
    if not user_data.is_dir() or is_reparse_point(user_data):
        return []
    found: list[Path] = []
    try:
        children = list(user_data.iterdir())
    except OSError:
        return []
    for profile in children:
        try:
            if not profile.is_dir() or is_reparse_point(profile):
                continue
        except OSError:
            continue
        if not _PROFILE_DIR.match(profile.name):
            continue
        for sub in _CHROMIUM_CACHE_DIRS:
            candidate = profile / sub
            if candidate.is_dir() and not is_reparse_point(candidate):
                found.append(candidate)
    for sub in _CHROMIUM_SHARED_CACHE_DIRS:
        candidate = user_data / sub
        if candidate.is_dir() and not is_reparse_point(candidate):
            found.append(candidate)
    return found


def _opera_caches(local_dirs: list[Path]) -> list[Path]:
    """Opera keeps cache folders on the profile directory itself."""
    found: list[Path] = []
    for local in local_dirs:
        for profile_name in ("Opera Stable", "Opera GX"):
            profile = local / "Opera Software" / profile_name
            if not profile.is_dir() or is_reparse_point(profile):
                continue
            for sub in _CHROMIUM_CACHE_DIRS:
                candidate = profile / sub
                if candidate.is_dir() and not is_reparse_point(candidate):
                    found.append(candidate)
    return found


def _firefox_caches(drive_roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    search_roots: list[Path] = []
    for base in _local_appdata_dirs(drive_roots) + _roaming_dirs(drive_roots):
        search_roots.append(base / "Mozilla" / "Firefox" / "Profiles")
    roaming = os.environ.get("APPDATA")
    if roaming:
        ini = Path(roaming) / "Mozilla" / "Firefox" / "profiles.ini"
        search_roots.extend(_firefox_ini_profiles(ini))
    for profiles in search_roots:
        if profiles.name != "Profiles":
            profile_dirs = [profiles]
        else:
            if not profiles.is_dir():
                continue
            try:
                profile_dirs = [p for p in profiles.iterdir() if p.is_dir()]
            except OSError:
                continue
        for profile in profile_dirs:
            if is_reparse_point(profile):
                continue
            for sub in ("cache2", "startupCache"):
                candidate = profile / sub
                try:
                    if candidate.is_dir() and not is_reparse_point(candidate) and _is_firefox_cache(candidate):
                        found.append(candidate)
                except OSError:
                    continue
    return found


def _is_firefox_cache(path: Path) -> bool:
    """Keep a profiles.ini path from pointing the cleaner at an unrelated folder."""
    parts = [part.casefold() for part in path.parts]
    if "mozilla" not in parts or "firefox" not in parts:
        return False
    return path.name.casefold() in {"cache2", "startupcache"}


def _firefox_ini_profiles(ini: Path) -> list[Path]:
    if not ini.is_file():
        return []
    try:
        text = ini.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    profiles: list[Path] = []
    current: dict[str, str] = {}

    def finish() -> None:
        path_text = current.get("Path")
        if not path_text:
            return
        if current.get("IsRelative", "1") == "1":
            profiles.append(ini.parent / path_text)
        else:
            profiles.append(Path(path_text))

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            finish()
            current = {}
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            current[key.strip()] = value.strip()
    finish()
    return profiles


def _app_cache_dirs(parents: list[Path], parts: tuple[str, ...], subdirs: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for parent in parents:
        base = parent.joinpath(*parts)
        for sub in subdirs:
            candidate = base / sub
            try:
                if candidate.is_dir() and not is_reparse_point(candidate):
                    found.append(candidate)
            except OSError:
                continue
    return found


def _steam_htmlcache() -> list[Path]:
    candidates: list[Path] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            value, _ = winreg.QueryValueEx(key, "SteamPath")
            candidates.append(Path(value) / "htmlcache")
    except OSError:
        pass
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates.append(Path(program_files_x86) / "Steam" / "htmlcache")
    candidates.append(Path(program_files) / "Steam" / "htmlcache")
    return candidates


def _keep_safe(paths: list[Path], *, allow_program_files_cache: bool, label: str, warnings: list[str]) -> tuple[Path, ...]:
    kept: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            if not path.is_dir():
                continue
        except OSError as exc:
            warnings.append(f"{label}: could not read {path} ({exc})")
            continue
        allowed, reason = root_is_allowed(path, allow_program_files_cache=allow_program_files_cache)
        if not allowed:
            warnings.append(f"{label}: skipped {path} ({reason})")
            continue
        try:
            resolved = normalize(path)
        except OSError as exc:
            warnings.append(f"{label}: skipped {path} ({exc})")
            continue
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        kept.append(resolved)
    return tuple(kept)


def discover_targets(drive_roots: list[Path]) -> tuple[list[CategoryPlan], list[str]]:
    warnings: list[str] = []
    local_dirs = _existing_dirs(_local_appdata_dirs(drive_roots))
    roaming_dirs = _existing_dirs(_roaming_dirs(drive_roots))

    def plan(category_id, label, description, paths, **kwargs) -> CategoryPlan:
        allow = kwargs.get("allow_program_files_cache", False)
        roots = _keep_safe(paths, allow_program_files_cache=allow, label=label, warnings=warnings)
        return CategoryPlan(category_id, label, description, roots, **kwargs)

    chromium = (
        ("chrome", "Google Chrome cache", "Browser cache only. Cookies, passwords, history, and bookmarks stay.", ("Google", "Chrome", "User Data")),
        ("edge", "Microsoft Edge cache", "Browser cache only. Cookies, passwords, history, and bookmarks stay.", ("Microsoft", "Edge", "User Data")),
        ("brave", "Brave cache", "Browser cache only. Cookies, passwords, history, and bookmarks stay.", ("BraveSoftware", "Brave-Browser", "User Data")),
        ("vivaldi", "Vivaldi cache", "Browser cache only. Cookies, passwords, history, and bookmarks stay.", ("Vivaldi", "User Data")),
    )
    browser_plans = []
    for category_id, label, description, parts in chromium:
        paths: list[Path] = []
        for local in local_dirs:
            paths.extend(_chromium_caches(local.joinpath(*parts)))
        browser_plans.append(plan(category_id, label, description, paths))

    shader_paths: list[Path] = []
    package_paths: list[Path] = []
    inet_paths: list[Path] = []
    thumb_paths: list[Path] = []
    for local in local_dirs:
        shader_paths.extend(
            [
                local / "D3DSCache",
                local / "NVIDIA" / "DXCache",
                local / "NVIDIA" / "GLCache",
                local / "AMD" / "DxCache",
                local / "AMD" / "GLCache",
            ]
        )
        package_paths.extend(
            [
                local / "pip" / "Cache",
                local / "npm-cache",
                local / "NuGet" / "v3-cache",
                local / "NuGet" / "http-cache",
            ]
        )
        inet_paths.append(local / "Microsoft" / "Windows" / "INetCache")
        thumb_paths.append(local / "Microsoft" / "Windows" / "Explorer")
    for roaming in roaming_dirs:
        package_paths.append(roaming / "npm-cache")

    app_subdirs = ("Cache", "Code Cache", "GPUCache")
    categories = [
        plan(
            "user_temp",
            "User temporary files",
            "Files apps leave in your Temp folder. Files changed in the last hour are left alone.",
            _user_temp_candidates(drive_roots),
            min_age_seconds=TEMP_MIN_AGE_SECONDS,
        ),
        plan(
            "windows_temp",
            "Windows temporary files",
            "Files in Windows\\Temp that this account can read. The Temp folder itself stays.",
            _windows_temp_candidates(drive_roots),
            min_age_seconds=TEMP_MIN_AGE_SECONDS,
        ),
        plan(
            "inet_cache",
            "Windows internet cache",
            "Cached copies of web files from the built-in Windows internet cache.",
            inet_paths,
        ),
        plan(
            "thumbnails",
            "Thumbnail and icon cache",
            "Explorer rebuilds these the next time you open a folder.",
            thumb_paths,
            name_globs=("thumbcache_*.db", "iconcache_*.db"),
            recursive=False,
        ),
        plan(
            "shaders",
            "Graphics shader cache",
            "DirectX, NVIDIA, and AMD shader caches. Games rebuild them, which can cause a one-time stutter.",
            shader_paths,
        ),
        plan(
            "packages",
            "Package manager caches",
            "pip, npm, and NuGet download caches. Project folders and the pnpm store are not included.",
            package_paths,
        ),
        *browser_plans,
        plan(
            "opera",
            "Opera cache",
            "Opera Stable and Opera GX cache only. Saved passwords and bookmarks stay.",
            _opera_caches(local_dirs),
        ),
        plan(
            "firefox",
            "Firefox cache",
            "Firefox cache2 and startupCache only. Logins, cookies, and bookmarks stay.",
            _firefox_caches(drive_roots),
        ),
        plan(
            "discord",
            "Discord cache",
            "Discord's cache folders. Messages and account data stay.",
            _app_cache_dirs(roaming_dirs, ("discord",), app_subdirs),
        ),
        plan(
            "slack",
            "Slack cache",
            "Slack's cache folders. Workspaces and downloads stay.",
            _app_cache_dirs(roaming_dirs, ("Slack",), app_subdirs),
        ),
        plan(
            "teams",
            "Microsoft Teams cache",
            "Classic Teams cache folders. Chats and files stay.",
            _app_cache_dirs(roaming_dirs, ("Microsoft", "Teams"), app_subdirs),
        ),
        plan(
            "vscode",
            "Visual Studio Code cache",
            "Cache and CachedData only. Settings, extensions, and your projects stay.",
            _app_cache_dirs(
                roaming_dirs,
                ("Code",),
                ("Cache", "CachedData", "GPUCache", "Code Cache"),
            ),
        ),
        plan(
            "steam_html",
            "Steam web cache",
            "Steam's htmlcache only. Installed games in steamapps are not included.",
            _steam_htmlcache(),
            allow_program_files_cache=True,
        ),
    ]
    return categories, warnings
