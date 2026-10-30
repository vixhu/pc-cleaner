"""Desktop window for scanning and cleaning known caches."""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from cleaner.cleanup import clean, write_log
from cleaner.scanner import format_bytes, list_drives, refresh_free_space, scan
from cleaner.targets import discover_targets

LOG_PATH = Path(__file__).resolve().parent.parent / "clean-log.txt"
LARGEST_SHOWN = 8


class CleanerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("PC Cleaner")
        self.root.minsize(760, 560)
        self.root.geometry("880x680")
        self.queue: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.cleaning = False
        self.scanning = False
        self.report = None
        self.checks: dict[str, tk.BooleanVar] = {}
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll)

    def _build(self) -> None:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Body.TLabel", font=("Segoe UI", 10))
        style.configure("Muted.TLabel", font=("Segoe UI", 9), foreground="#4b5563")
        style.configure("Size.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Clean.TButton", font=("Segoe UI", 10, "bold"))

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=(16, 14, 16, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="PC Cleaner", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text=(
                "Shows cache and temporary files Windows and apps can rebuild, then deletes "
                "only the categories you select. Documents, Desktop, Downloads, pictures, "
                "videos, music, and installed programs are not touched. Freeing this space "
                "helps when a drive is nearly full. It does not change startup programs."
            ),
            style="Body.TLabel",
            wraplength=820,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", pady=(6, 10))

        buttons = ttk.Frame(header)
        buttons.grid(row=2, column=0, sticky="w")
        self.scan_button = ttk.Button(buttons, text="Scan", command=self.start_scan)
        self.scan_button.grid(row=0, column=0, padx=(0, 8))
        self.select_button = ttk.Button(buttons, text="Select all", command=self.select_all, state="disabled")
        self.select_button.grid(row=0, column=1, padx=(0, 8))
        self.clear_button = ttk.Button(buttons, text="Clear", command=self.clear_selection, state="disabled")
        self.clear_button.grid(row=0, column=2)

        self.status = ttk.Label(header, text="Press Scan to check every ready drive.", style="Muted.TLabel")
        self.status.grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.drives_label = ttk.Label(header, text="", style="Muted.TLabel", wraplength=820, justify="left")
        self.drives_label.grid(row=4, column=0, sticky="ew", pady=(2, 0))

        holder = ttk.Frame(self.root, padding=(16, 0, 16, 0))
        holder.grid(row=1, column=0, sticky="nsew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(holder, highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.rows = ttk.Frame(self.canvas)
        self.rows.columnconfigure(0, weight=1)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.rows, anchor="nw")
        self.rows.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._fit_canvas)
        self.canvas.bind("<Enter>", lambda _event: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda _event: self.canvas.unbind_all("<MouseWheel>"))

        footer = ttk.Frame(self.root, padding=(16, 10, 16, 14))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.total_label = ttk.Label(footer, text="Selected: 0 B", style="Size.TLabel")
        self.total_label.grid(row=0, column=0, sticky="w")
        self.clean_button = ttk.Button(
            footer,
            text="Clean selected",
            style="Clean.TButton",
            command=self.start_clean,
            state="disabled",
        )
        self.clean_button.grid(row=0, column=1, sticky="e")

    def _fit_canvas(self, event) -> None:
        self.canvas.itemconfigure(self.canvas_window, width=event.width)

    def _wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _on_close(self) -> None:
        if self.cleaning:
            messagebox.showinfo("PC Cleaner", "Cleaning is still running. You can close this window when it finishes.")
            return
        self.cancel.set()
        self.root.destroy()

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.scan_button.configure(state=state)
        self.select_button.configure(state=state if self.report else "disabled")
        self.clear_button.configure(state=state if self.report else "disabled")
        if busy:
            self.clean_button.configure(state="disabled")
        else:
            self._update_total()

    def start_scan(self) -> None:
        if self.scanning or self.cleaning:
            return
        self.scanning = True
        self.cancel.clear()
        self._set_busy(True)
        self.status.configure(text="Checking drives…")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self) -> None:
        try:
            drives = list_drives()
            self.queue.put(("progress", "Checking known cache locations…"))
            ready = [drive.root for drive in drives if drive.ready]
            categories, warnings = discover_targets(ready)
            report = scan(
                categories,
                drives,
                progress=lambda message: self.queue.put(("progress", message)),
                cancel=self.cancel,
            )
            report.warnings.extend(warnings)
            self.queue.put(("scan_done", report))
        except Exception as exc:
            self.queue.put(("scan_error", str(exc)))

    def start_clean(self) -> None:
        if self.report is None or self.scanning or self.cleaning:
            return
        chosen = [category for category in self.report.categories if self.checks.get(category.category_id) and self.checks[category.category_id].get() and category.entries]
        if not chosen:
            messagebox.showinfo("PC Cleaner", "Select at least one category that has files to remove.")
            return
        lines = [
            f"• {category.label}: {format_bytes(category.size)} ({len(category.entries):,} files)"
            for category in chosen
        ]
        total = sum(category.size for category in chosen)
        message = (
            f"This will permanently delete {format_bytes(total)} of cache and temporary files:\n\n"
            + "\n".join(lines)
            + "\n\nDocuments, Desktop, Downloads, pictures, videos, music, and installed programs "
            "are not included. Files changed in the last hour in Temp are left alone.\n\n"
            "Windows and your apps can rebuild these caches. Continue?"
        )
        if not messagebox.askokcancel("Clean selected cache?", message, icon="warning", default="cancel"):
            return
        entries = [entry for category in chosen for entry in category.entries]
        self.cleaning = True
        self._set_busy(True)
        self.status.configure(text="Cleaning selected caches…")
        threading.Thread(target=self._clean_worker, args=(entries,), daemon=True).start()

    def _clean_worker(self, entries) -> None:
        try:
            result = clean(entries, now=time.time())
            log_error = ""
            try:
                write_log(LOG_PATH, result)
            except OSError as exc:
                log_error = str(exc)
            drives = list_drives()
            self.queue.put(("progress", "Checking what is left…"))
            ready = [drive.root for drive in drives if drive.ready]
            categories, warnings = discover_targets(ready)
            report = scan(
                categories,
                drives,
                progress=lambda message: self.queue.put(("progress", message)),
            )
            report.warnings.extend(warnings)
            self.queue.put(("clean_done", (result, report, log_error)))
        except Exception as exc:
            self.queue.put(("clean_error", str(exc)))

    def select_all(self) -> None:
        for category_id, variable in self.checks.items():
            category = self._category(category_id)
            if category and category.entries:
                variable.set(True)

    def clear_selection(self) -> None:
        for variable in self.checks.values():
            variable.set(False)

    def _category(self, category_id: str):
        if self.report is None:
            return None
        for category in self.report.categories:
            if category.category_id == category_id:
                return category
        return None

    def _update_total(self) -> None:
        if self.report is None:
            self.total_label.configure(text="Selected: 0 B")
            self.clean_button.configure(state="disabled")
            return
        total = 0
        files = 0
        for category in self.report.categories:
            variable = self.checks.get(category.category_id)
            if variable is not None and variable.get():
                total += category.size
                files += len(category.entries)
        self.total_label.configure(text=f"Selected: {format_bytes(total)} in {files:,} files")
        allowed = "normal" if files and not self.scanning and not self.cleaning else "disabled"
        self.clean_button.configure(state=allowed)

    def _render(self, report) -> None:
        self.report = report
        self.checks.clear()
        for child in self.rows.winfo_children():
            child.destroy()
        self.drives_label.configure(text=_drive_text(report.drives))
        start = 0
        if report.warnings:
            shown = report.warnings[:4]
            extra = len(report.warnings) - len(shown)
            warning_text = "Skipped unsafe locations: " + " | ".join(shown)
            if extra > 0:
                warning_text += f" | and {extra} more"
            ttk.Label(self.rows, text=warning_text, style="Muted.TLabel", wraplength=780, justify="left").grid(
                row=0, column=0, sticky="ew", pady=(0, 8)
            )
            start = 1
        for index, category in enumerate(report.categories, start=start):
            self._render_category(index, category)
        note = "Scan cancelled. The list above is only what was measured before the stop." if report.cancelled else ""
        if note:
            self.status.configure(text=note)
        self._update_total()

    def _render_category(self, row: int, category) -> None:
        frame = ttk.Frame(self.rows, padding=(0, 8))
        frame.grid(row=row, column=0, sticky="ew")
        frame.columnconfigure(1, weight=1)
        variable = tk.BooleanVar(value=False)
        self.checks[category.category_id] = variable
        variable.trace_add("write", lambda *_args: self._update_total())
        checkbox = ttk.Checkbutton(frame, variable=variable)
        checkbox.grid(row=0, column=0, sticky="nw", padx=(0, 8))
        if not category.entries:
            checkbox.state(["disabled"])
        title = ttk.Frame(frame)
        title.grid(row=0, column=1, sticky="ew")
        title.columnconfigure(0, weight=1)
        ttk.Label(title, text=category.label, style="Body.TLabel").grid(row=0, column=0, sticky="w")
        size_text = format_bytes(category.size) if category.entries else _empty_text(category)
        ttk.Label(title, text=f"{size_text}   ·   {len(category.entries):,} files", style="Size.TLabel").grid(row=0, column=1, sticky="e")
        ttk.Label(frame, text=category.description, style="Muted.TLabel", wraplength=760, justify="left").grid(
            row=1, column=1, sticky="ew", pady=(2, 0)
        )
        notes = []
        if category.skipped_recent:
            notes.append(f"{category.skipped_recent:,} files changed in the last hour were left alone.")
        if category.skipped_unsafe:
            notes.append(f"{category.skipped_unsafe:,} items were blocked by the safety check.")
        if category.unreadable:
            notes.append(f"{category.unreadable:,} items or folders could not be read and were left alone.")
        if notes:
            ttk.Label(frame, text=" ".join(notes), style="Muted.TLabel", wraplength=760, justify="left").grid(
                row=2, column=1, sticky="ew"
            )
        if category.entries:
            details = ttk.Frame(frame)
            lines = [
                f"{format_bytes(entry.size):>10}   {entry.path}"
                for entry in category.entries[:LARGEST_SHOWN]
            ]
            if len(category.entries) > LARGEST_SHOWN:
                lines.append(f"… and {len(category.entries) - LARGEST_SHOWN:,} smaller files")
            ttk.Label(details, text="\n".join(lines), style="Muted.TLabel", justify="left").grid(row=0, column=0, sticky="w")
            open_details = tk.BooleanVar(value=False)

            def toggle(holder=details, flag=open_details) -> None:
                if flag.get():
                    holder.grid_remove()
                    flag.set(False)
                else:
                    holder.grid(row=4, column=1, sticky="ew", pady=(4, 0))
                    flag.set(True)

            ttk.Button(frame, text="Largest files", command=toggle).grid(row=3, column=1, sticky="w", pady=(4, 0))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "progress":
                    self.status.configure(text=payload)
                elif kind == "scan_done":
                    self.scanning = False
                    self._render(payload)
                    if not payload.cancelled:
                        found = sum(category.size for category in payload.categories)
                        self.status.configure(text=f"Scan finished. {format_bytes(found)} can be removed. Nothing was deleted.")
                    self._set_busy(False)
                elif kind == "scan_error":
                    self.scanning = False
                    self._set_busy(False)
                    self.status.configure(text="Scan failed.")
                    messagebox.showerror("PC Cleaner", payload)
                elif kind == "clean_done":
                    result, report, log_error = payload
                    self.cleaning = False
                    refresh_free_space(report.drives)
                    self._render(report)
                    self._set_busy(False)
                    self.status.configure(text=f"Removed {format_bytes(result.deleted_bytes)}. A new scan is showing what is left.")
                    self._show_clean_result(result, report, log_error)
                elif kind == "clean_error":
                    self.cleaning = False
                    self._set_busy(False)
                    self.status.configure(text="Clean failed.")
                    messagebox.showerror("PC Cleaner", payload)
        except queue.Empty:
            pass
        try:
            self.root.after(100, self._poll)
        except tk.TclError:
            return

    def _show_clean_result(self, result, report, log_error: str) -> None:
        space = _drive_text(report.drives)
        log_line = f"Log: {LOG_PATH}" if not log_error else f"The clean log could not be written ({log_error})."
        message = (
            f"Removed {result.deleted_count:,} files, freeing {format_bytes(result.deleted_bytes)}.\n"
            f"Skipped {len(result.skipped):,} files that were locked, too new, or not allowed.\n"
            f"Failed: {len(result.errors):,}.\n"
            f"Empty cache folders removed: {result.removed_dirs:,}.\n\n"
            f"{log_line}\n\n{space}"
        )
        messagebox.showinfo("Clean finished", message)


def _empty_text(category) -> str:
    if category.roots_found == 0:
        return "Not on this PC"
    if category.unreadable:
        return "Could not read"
    return "Nothing to remove"


def _drive_text(drives) -> str:
    parts = []
    for drive in drives:
        if drive.ready and drive.free_bytes is not None and drive.total_bytes is not None:
            parts.append(
                f"{drive.root} {drive.kind.lower()}, {format_bytes(drive.free_bytes)} free of {format_bytes(drive.total_bytes)}"
            )
        else:
            parts.append(f"{drive.root} {drive.kind.lower()}, {drive.note or 'not scanned'}")
    return "Drives: " + "; ".join(parts)


def main() -> None:
    root = tk.Tk()
    CleanerApp(root)
    root.mainloop()
