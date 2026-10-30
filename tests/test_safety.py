"""Safety checks for the cache cleaner. These tests do not clean the real PC."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cleaner.cleanup import clean
from cleaner.safety import (
    deletion_allowed,
    is_steam_htmlcache,
    root_is_allowed,
    touches_personal_or_system,
)
from cleaner.scanner import DriveInfo, FileEntry, list_drives, scan
from cleaner.targets import CategoryPlan, discover_targets


def _sandbox() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "Temp" / "pc-cleaner-tests"
    base.mkdir(parents=True, exist_ok=True)
    if touches_personal_or_system(base):
        raise unittest.SkipTest(f"test folder is inside a protected path: {base}")
    return base


def _entry(path: Path, root: Path, *, min_age: int | None = None, allow_program_files: bool = False) -> FileEntry:
    return FileEntry(
        path=path,
        size=path.stat().st_size if path.is_file() else 0,
        root=root,
        category_id="test",
        min_age_seconds=min_age,
        allow_program_files_cache=allow_program_files,
    )


class SafetyTests(unittest.TestCase):
    def test_temp_file_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory(dir=_sandbox()) as folder:
            root = Path(folder)
            target = root / "cache.bin"
            target.write_bytes(b"1234")
            allowed, reason = deletion_allowed(
                target, root, allow_program_files_cache=False, min_age_seconds=None, now=time.time()
            )
            self.assertTrue(allowed, reason)

    def test_parent_escape_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(dir=_sandbox()) as folder:
            base = Path(folder)
            root = base / "temp"
            root.mkdir()
            secret = base / "secret.txt"
            secret.write_text("keep", encoding="utf-8")
            escape = root / ".." / "secret.txt"
            allowed, reason = deletion_allowed(
                escape, root, allow_program_files_cache=False, min_age_seconds=None, now=time.time()
            )
            self.assertFalse(allowed, reason)
            self.assertEqual(reason, "outside the cache folder")
            self.assertTrue(secret.exists())

    def test_symlink_escape_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(dir=_sandbox()) as folder:
            base = Path(folder)
            root = base / "temp"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            secret = outside / "secret.txt"
            secret.write_text("keep", encoding="utf-8")
            link = root / "link.txt"
            try:
                os.symlink(secret, link)
            except OSError:
                self.skipTest("creating a symlink needs Developer Mode or an elevated shell")
            allowed, _reason = deletion_allowed(
                link, root, allow_program_files_cache=False, min_age_seconds=None, now=time.time()
            )
            self.assertFalse(allowed)
            self.assertTrue(secret.exists())

    def test_personal_folders_are_refused(self) -> None:
        samples = [
            Path(r"C:\Users\V\Documents\notes.txt"),
            Path(r"C:\Users\V\Desktop\photo.png"),
            Path(r"C:\Users\V\Downloads\setup.exe"),
            Path(r"C:\Users\V\Pictures\trip.jpg"),
            Path(r"C:\Users\V\Videos\clip.mp4"),
            Path(r"C:\Users\V\Music\song.mp3"),
            Path(r"C:\Users\V\OneDrive\Desktop\pc-cleaner\main.py"),
            Path(r"D:\Users\V\My Documents\letter.doc"),
            Path(r"C:\Windows\System32\kernel32.dll"),
            Path(r"C:\Windows\SysWOW64\kernel32.dll"),
            Path(r"C:\Windows\WinSxS\backup\file.dll"),
        ]
        for path in samples:
            self.assertTrue(touches_personal_or_system(path), path)
            allowed, reason = deletion_allowed(
                path, path.parent, allow_program_files_cache=False, min_age_seconds=None, now=time.time()
            )
            self.assertFalse(allowed, path)
            self.assertIn("personal or system", reason)

    def test_browser_cookie_file_is_refused_even_inside_a_cache(self) -> None:
        with tempfile.TemporaryDirectory(dir=_sandbox()) as folder:
            root = Path(folder)
            cookies = root / "Cookies"
            cookies.write_bytes(b"nope")
            allowed, reason = deletion_allowed(
                cookies, root, allow_program_files_cache=False, min_age_seconds=None, now=time.time()
            )
            self.assertFalse(allowed)
            self.assertEqual(reason, "profile or system file")

    def test_program_files_and_steam_htmlcache(self) -> None:
        program = Path(r"C:\Program Files\Steam\htmlcache")
        allowed, reason = root_is_allowed(program, allow_program_files_cache=False)
        self.assertFalse(allowed, reason)
        steam = Path(r"C:\Program Files (x86)\Steam\htmlcache")
        allowed, reason = root_is_allowed(steam, allow_program_files_cache=True)
        self.assertTrue(allowed, reason)
        self.assertTrue(is_steam_htmlcache(steam))
        games = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Game")
        allowed, _reason = root_is_allowed(games, allow_program_files_cache=True)
        self.assertFalse(allowed)
        self.assertFalse(is_steam_htmlcache(games))

    def test_cache_root_itself_is_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory(dir=_sandbox()) as folder:
            root = Path(folder)
            kept = root / "keep.txt"
            kept.write_text("safe", encoding="utf-8")
            result = clean([_entry(root, root)], now=time.time())
            self.assertEqual(result.deleted_count, 0)
            self.assertTrue(root.exists())
            self.assertTrue(kept.exists())

    def test_recent_temp_file_is_kept_and_old_file_is_removed(self) -> None:
        with tempfile.TemporaryDirectory(dir=_sandbox()) as folder:
            root = Path(folder)
            recent = root / "recent.tmp"
            old = root / "old.tmp"
            recent.write_bytes(b"ab")
            old.write_bytes(b"xyz")
            now = time.time()
            os.utime(recent, (now, now))
            os.utime(old, (now - 7200, now - 7200))
            result = clean(
                [_entry(recent, root, min_age=3600), _entry(old, root, min_age=3600)],
                now=now,
            )
            self.assertTrue(recent.exists())
            self.assertFalse(old.exists())
            self.assertEqual(result.deleted_count, 1)
            self.assertEqual(result.deleted_bytes, 3)
            self.assertTrue(root.exists())

    def test_cleanup_keeps_files_outside_the_root_and_removes_empty_child_folders(self) -> None:
        with tempfile.TemporaryDirectory(dir=_sandbox()) as folder:
            base = Path(folder)
            root = base / "cache"
            child = root / "sub"
            child.mkdir(parents=True)
            inside = child / "a.bin"
            inside.write_bytes(b"1234")
            outside = base / "keep.txt"
            outside.write_text("keep", encoding="utf-8")
            cookies = root / "Cookies"
            cookies.write_bytes(b"secret")
            result = clean(
                [
                    _entry(inside, root),
                    _entry(outside, root),
                    _entry(cookies, root),
                    _entry(root, root),
                ],
                now=time.time(),
            )
            self.assertFalse(inside.exists())
            self.assertFalse(child.exists())
            self.assertTrue(root.exists())
            self.assertTrue(outside.exists())
            self.assertTrue(cookies.exists())
            self.assertEqual(result.deleted_count, 1)
            self.assertEqual(result.removed_dirs, 1)


class ScannerTests(unittest.TestCase):
    def test_scan_measures_old_files_and_skips_recent_links_and_profile_files(self) -> None:
        with tempfile.TemporaryDirectory(dir=_sandbox()) as folder:
            base = Path(folder)
            root = base / "cache"
            root.mkdir()
            old = root / "old.bin"
            recent = root / "recent.bin"
            cookies = root / "Cookies"
            old.write_bytes(b"12345")
            recent.write_bytes(b"ab")
            cookies.write_bytes(b"no")
            now = time.time()
            os.utime(old, (now - 7200, now - 7200))
            os.utime(recent, (now, now))
            outside = base / "only-outside.txt"
            outside.write_text("nope", encoding="utf-8")
            link = root / "linked-dir"
            try:
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(root.parent)],
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError:
                link = None
            category = CategoryPlan(
                category_id="fixture",
                label="Fixture",
                description="Test cache",
                roots=(root,),
                min_age_seconds=3600,
            )
            report = scan([category], drives=[], now=now)
            names = {entry.path.name for entry in report.categories[0].entries}
            self.assertEqual(names, {"old.bin"})
            self.assertEqual(report.categories[0].size, 5)
            self.assertGreaterEqual(report.categories[0].skipped_recent, 1)
            self.assertTrue(cookies.exists())
            self.assertTrue(outside.exists())
            if link is not None:
                self.assertNotIn("only-outside.txt", names)

    def test_discovered_roots_stay_on_the_whitelist(self) -> None:
        drives = list_drives()
        ready = [drive.root for drive in drives if drive.ready]
        self.assertTrue(any(drive.kind == "Fixed" and drive.ready for drive in drives))
        categories, _warnings = discover_targets(ready)
        self.assertGreaterEqual(len(categories), 10)
        for category in categories:
            for root in category.roots:
                allowed, reason = root_is_allowed(
                    root, allow_program_files_cache=category.allow_program_files_cache
                )
                self.assertTrue(allowed, f"{root} ({reason})")
                self.assertFalse(touches_personal_or_system(root), root)


class UiTests(unittest.TestCase):
    def test_window_builds_without_scanning(self) -> None:
        import tkinter as tk

        from cleaner.app import CleanerApp

        root = tk.Tk()
        root.withdraw()
        app = CleanerApp(root)
        root.update_idletasks()
        self.assertEqual(str(app.scan_button["state"]), "normal")
        self.assertEqual(str(app.clean_button["state"]), "disabled")
        self.assertIsNone(app.report)
        root.destroy()


class DriveTests(unittest.TestCase):
    def test_list_drives_marks_fixed_disks(self) -> None:
        drives = list_drives()
        self.assertTrue(drives)
        self.assertTrue(all(isinstance(drive, DriveInfo) for drive in drives))
        self.assertTrue(any(drive.ready and drive.free_bytes is not None for drive in drives))


if __name__ == "__main__":
    unittest.main()
