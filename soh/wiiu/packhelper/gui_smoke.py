#!/usr/bin/env python3
"""Headless screenshot and responsiveness smoke test for PackHelper's wizard.

Run this from a repository checkout under an X display (normally Xvfb).  The
test uses only temporary card/pack data and replaces every native dialog, so it
cannot select or modify a real SD card.
"""

from __future__ import annotations

import io
import json
import struct
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import ImageGrab


SCRIPT = Path(__file__).resolve()
WIIU_ROOT = SCRIPT.parents[1]
REPOSITORY_ROOT = WIIU_ROOT.parent
SCREENSHOT_DIR = WIIU_ROOT / ".work" / "packhelper-shots"
TK_LIMIT_SECONDS = 0.2

if str(WIIU_ROOT) not in sys.path:
    sys.path.insert(0, str(WIIU_ROOT))

from packhelper import packhelper  # noqa: E402


def _write_archive(path: Path, members: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def _nested_archive(members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return output.getvalue()


def _synthetic_files(base: Path) -> tuple[Path, Path, list[Path], Path]:
    card = base / "synthetic-sd"
    game = card / "wiiu" / "apps" / "soh923"
    game.mkdir(parents=True)
    header = b"\x00\x00\x00\x00TLDO" + b"\x00" * 56
    _write_archive(
        game / "oot.o2r",
        {
            "portVersion": b"\x01" + struct.pack(">HHH", 9, 2, 3),
            "root/resource": header,
        },
    )

    old_mods = game / "mods"
    old_mods.mkdir()
    (old_mods / "Djipi's 3DE - Old Textures.o2r").write_bytes(b"old Djipi pack")
    (old_mods / "Unrelated Pack.o2r").write_bytes(b"unrelated pack")
    backup = game / "mods-backup-20261004-120000"

    skilar = base / "Skilar-Art-Plus-Link.o2r"
    _write_archive(skilar, {"alt/synthetic": header})

    # The second download has a generic outer name.  Its familiar pack name is
    # available only in ZIP member names, which exercises the cheap step-2 path.
    djipi = base / "texture-pack-download.zip"
    _write_archive(
        djipi,
        {
            "Djipi 3DS Experience/01 Main Textures.o2r": _nested_archive(
                {"alt/djipi-synthetic": header}
            )
        },
    )
    return card, game, [skilar, djipi], backup


class TkTimer:
    """Measure Python callbacks entered from Tk, plus actions invoked by us."""

    def __init__(self, root: object) -> None:
        self.root = root
        self.main_thread = threading.get_ident()
        self.samples: list[tuple[str, float]] = []
        self.callback_errors: list[BaseException] = []
        self._after = root.after

        def measured_after(delay: int, callback: Callable[..., object] | None = None, *args: object):
            if callback is None:
                return self._after(delay)

            callback_name = getattr(callback, "__qualname__", repr(callback))

            def measured_callback() -> object:
                started = time.perf_counter()
                try:
                    return callback(*args)
                except BaseException as error:
                    self.callback_errors.append(error)
                    raise
                finally:
                    self.samples.append((f"after:{callback_name}", time.perf_counter() - started))

            return self._after(delay, measured_callback)

        root.after = measured_after

    def call(self, name: str, callback: Callable[[], object]) -> object:
        if threading.get_ident() != self.main_thread:
            raise AssertionError(f"Tk action {name!r} was not run on the Tk thread")
        started = time.perf_counter()
        try:
            return callback()
        finally:
            self.samples.append((name, time.perf_counter() - started))

    @property
    def maximum(self) -> float:
        return max((elapsed for _name, elapsed in self.samples), default=0.0)

    def assert_fast(self) -> None:
        if self.callback_errors:
            raise AssertionError(f"Tk callback failed: {self.callback_errors[0]!r}")
        slow = [(name, elapsed) for name, elapsed in self.samples if elapsed > TK_LIMIT_SECONDS]
        if slow:
            name, elapsed = max(slow, key=lambda item: item[1])
            raise AssertionError(
                f"Tk-thread call {name!r} took {elapsed:.3f}s; limit is {TK_LIMIT_SECONDS:.3f}s"
            )


class PatchedDialogs:
    """Deterministic, non-blocking replacements for all native dialogs."""

    def __init__(self, root: object, card: Path, packs: list[Path]) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.root = root
        self.tk = tk
        self.ttk = ttk
        self.card = card
        self.packs = packs
        self.calls: list[tuple[str, str]] = []
        self.error_window: object | None = None
        self._originals = {
            "askdirectory": filedialog.askdirectory,
            "askopenfilenames": filedialog.askopenfilenames,
            "showerror": messagebox.showerror,
            "showinfo": messagebox.showinfo,
        }

        filedialog.askdirectory = self.askdirectory
        filedialog.askopenfilenames = self.askopenfilenames
        messagebox.showerror = self.showerror
        messagebox.showinfo = self.showinfo
        self.filedialog = filedialog
        self.messagebox = messagebox

    def restore(self) -> None:
        self.filedialog.askdirectory = self._originals["askdirectory"]
        self.filedialog.askopenfilenames = self._originals["askopenfilenames"]
        self.messagebox.showerror = self._originals["showerror"]
        self.messagebox.showinfo = self._originals["showinfo"]

    def askdirectory(self, **_kwargs: object) -> str:
        self.calls.append(("askdirectory", str(self.card)))
        return str(self.card)

    def askopenfilenames(self, **_kwargs: object) -> tuple[str, ...]:
        self.calls.append(("askopenfilenames", " ".join(map(str, self.packs))))
        return tuple(map(str, self.packs))

    def showinfo(self, _title: str, message: str, **_kwargs: object) -> str:
        self.calls.append(("showinfo", str(message)))
        return "ok"

    def showerror(self, title: str, message: str, **_kwargs: object) -> str:
        self.calls.append(("showerror", str(message)))
        if self.error_window is not None and self.error_window.winfo_exists():
            self.error_window.destroy()
        dialog = self.tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("520x170+200+220")
        dialog.transient(self.root)
        self.ttk.Label(dialog, text="Something went wrong", font=("TkDefaultFont", 13, "bold")).pack(
            anchor="w", padx=22, pady=(20, 8)
        )
        self.ttk.Label(dialog, text=str(message), wraplength=470).pack(anchor="w", padx=22)
        self.ttk.Button(dialog, text="OK", command=dialog.destroy).pack(side="bottom", pady=16)
        self.error_window = dialog
        return "ok"


def _walk(widget: object):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _widget_text(widget: object, root: object) -> str:
    try:
        if widget.winfo_class() == "Listbox":
            return "\n".join(str(item) for item in widget.get(0, "end"))
    except Exception:
        pass
    try:
        text = str(widget.cget("text"))
    except Exception:
        text = ""
    try:
        variable = str(widget.cget("textvariable"))
        if variable:
            text = str(root.getvar(variable))
    except Exception:
        pass
    return text


def _all_text(root: object) -> str:
    return "\n".join(filter(None, (_widget_text(widget, root) for widget in _walk(root))))


def _find_button(root: object, predicate: Callable[[str], bool]):
    for widget in _walk(root):
        try:
            if widget.winfo_class() in {"TButton", "Button"}:
                text = _widget_text(widget, root)
                if predicate(text):
                    return widget
        except Exception:
            continue
    raise AssertionError(f"button not found; visible text was:\n{_all_text(root)}")


def _pump(root: object, timer: TkTimer, seconds: float = 0.05) -> None:
    # Entering a real mainloop matters here: tkinter permits worker threads to
    # schedule ``after`` callbacks only while the owning thread is dispatching.
    root.after(max(1, round(seconds * 1000)), root.quit)
    root.mainloop()


def _wait_for(root: object, timer: TkTimer, condition: Callable[[], bool], message: str) -> None:
    outcome = {"done": False}

    def poll() -> None:
        if condition():
            outcome["done"] = True
            root.quit()
        else:
            root.after(10, poll)

    def timeout() -> None:
        root.quit()

    root.after(0, poll)
    timeout_id = root.after(5000, timeout)
    root.mainloop()
    if outcome["done"]:
        root.after_cancel(timeout_id)
        return
    raise AssertionError(message)


def _capture(root: object, name: str, extra_window: object | None = None) -> Path:
    root.update_idletasks()
    windows = [root]
    if extra_window is not None and extra_window.winfo_exists():
        extra_window.update_idletasks()
        windows.append(extra_window)
    left = min(window.winfo_rootx() for window in windows) - 8
    top = min(window.winfo_rooty() for window in windows) - 32
    right = max(window.winfo_rootx() + window.winfo_width() for window in windows) + 8
    bottom = max(window.winfo_rooty() + window.winfo_height() for window in windows) + 8
    image = ImageGrab.grab(bbox=(max(0, left), max(0, top), right, bottom))
    pixels = np.asarray(image.convert("RGB"), dtype=np.uint8)
    if pixels.size == 0 or image.width < 300 or image.height < 200 or float(pixels.std()) < 1.0:
        raise AssertionError(f"screenshot {name!r} is empty or visually blank")
    path = SCREENSHOT_DIR / name
    image.save(path, format="PNG")
    return path


def run() -> dict[str, object]:
    import tkinter as tk

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    screenshots: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="packhelper-gui-smoke-") as temp_name:
        card, game, packs, backup = _synthetic_files(Path(temp_name))
        card_without_soh = Path(temp_name) / "card-without-soh"
        (card_without_soh / "wiiu").mkdir(parents=True)
        card_without_oot = Path(temp_name) / "card-without-oot"
        (card_without_oot / "wiiu" / "apps" / "soh923").mkdir(parents=True)
        root = tk.Tk()
        root.geometry("720x480+80+60")
        timer = TkTimer(root)
        dialogs = PatchedDialogs(root, card, packs)
        original_check_free_space = packhelper.check_free_space
        original_select_packs = packhelper.select_packs
        original_run_install = packhelper.run_install
        release_install = threading.Event()
        worker_started = threading.Event()
        worker_threads: list[int] = []

        def guarded_select_packs(paths):
            if threading.get_ident() == timer.main_thread:
                raise AssertionError("deep pack inspection ran on the Tk thread")
            return original_select_packs(paths)

        def fake_run_install(sd_root, pack_paths, **kwargs):
            worker_threads.append(threading.get_ident())
            worker_started.set()
            selections = guarded_select_packs(pack_paths)
            progress = kwargs.get("progress")
            if progress:
                progress(12, "Checking the pack files...")
                progress(46, "Changing the packs so they work on Wii U...")
            if not release_install.wait(5.0):
                raise RuntimeError("smoke-test conversion was not released")
            if progress:
                progress(88, "Copying the finished packs to the SD card...")
            backup.mkdir()
            (game / "mods" / "Djipi's 3DE - Old Textures.o2r").rename(
                backup / "Djipi's 3DE - Old Textures.o2r"
            )
            return packhelper.InstallResult(
                packhelper.SdCard(Path(sd_root), game),
                backup,
                game / "mods",
                tuple(selection.label for selection in selections),
            )

        packhelper.select_packs = guarded_select_packs
        packhelper.run_install = fake_run_install
        try:
            window = timer.call("PackHelperWindow", lambda: packhelper.PackHelperWindow(root, []))
            timer.call("root.update_idletasks", root.update_idletasks)
            _pump(root, timer)
            startup_text = _all_text(root)
            assert "Choose your SD card" in startup_text
            assert "every 2 seconds" not in startup_text
            assert "Look again" not in startup_text
            _find_button(root, lambda text: text == "Choose your SD card...")
            next_button = _find_button(root, lambda text: text == "Next")
            assert str(next_button.cget("state")) == "disabled"
            assert not dialogs.calls, f"startup opened a dialog before user action: {dialogs.calls!r}"
            screenshots.append(_capture(root, "01-startup-no-card.png"))

            dialogs.card = card_without_soh
            choose_sd = _find_button(root, lambda text: text == "Choose your SD card...")
            timer.call("Choose card without SoH", choose_sd.invoke)
            _wait_for(
                root,
                timer,
                lambda: packhelper.SOH_NOT_INSTALLED in _all_text(root),
                "the no-SoH card message was not displayed",
            )
            assert str(_find_button(root, lambda text: text == "Next").cget("state")) == "disabled"
            screenshots.append(_capture(root, "card-without-soh.png"))

            dialogs.card = card_without_oot
            choose_sd = _find_button(root, lambda text: text == "Choose your SD card...")
            timer.call("Choose card without oot.o2r", choose_sd.invoke)
            _wait_for(
                root,
                timer,
                lambda: packhelper.OOT_MISSING in _all_text(root) and getattr(window, "card", None) is None,
                "the missing-oot.o2r message was not displayed",
            )
            screenshots.append(_capture(root, "missing-oot.png"))

            dialogs.card = card
            choose_sd = _find_button(root, lambda text: text == "Choose your SD card...")
            timer.call("Choose valid card", choose_sd.invoke)
            _wait_for(root, timer, lambda: getattr(window, "card", None) is not None, "SD card was not accepted")
            screenshots.append(_capture(root, "02-card-found.png"))

            next_button = _find_button(root, lambda text: text == "Next")
            timer.call("step-1 Next", next_button.invoke)
            _pump(root, timer)
            choose_packs = _find_button(
                root, lambda text: "pack" in text.casefold() and ("add" in text.casefold() or "choose" in text.casefold())
            )
            timer.call("choose pack files", choose_packs.invoke)
            _pump(root, timer)
            listed_text = _all_text(root)
            assert "Skilar" in listed_text and "Djipi" in listed_text, listed_text
            screenshots.append(_capture(root, "03-packs-listed.png"))

            next_button = _find_button(root, lambda text: text == "Next")
            timer.call("step-2 Next", next_button.invoke)
            _pump(root, timer)
            ready_text = _all_text(root)
            assert "5 to 10 minutes" in ready_text
            assert "keep the sd card" in ready_text.casefold()
            assert "GB free on this computer and on the SD card" in ready_text
            assert "Skilar's Art Plus Link will be added." in ready_text, ready_text
            assert "Djipi's 3DS Experience will be updated." in ready_text, ready_text
            assert "Your 1 other pack stays as it is." in ready_text, ready_text

            def reject_space(*_args: object, **_kwargs: object) -> None:
                raise packhelper.OutOfSpace()

            packhelper.check_free_space = reject_space
            try:
                blocked_start = _find_button(root, lambda text: text == "Start")
                timer.call("Start without space", blocked_start.invoke)
                _pump(root, timer)
                assert not worker_started.is_set(), "out-of-space preflight started conversion"
                assert dialogs.calls[-1] == ("showerror", packhelper.NOT_ENOUGH_SPACE)
                assert dialogs.error_window is not None
                timer.call("dismiss no-space error", dialogs.error_window.destroy)
            finally:
                packhelper.check_free_space = original_check_free_space
            screenshots.append(_capture(root, "04-ready.png"))

            start_button = _find_button(root, lambda text: text == "Start")
            timer.call("Start", start_button.invoke)
            _wait_for(root, timer, worker_started.is_set, "conversion worker did not start")
            _wait_for(
                root,
                timer,
                lambda: "Changing the packs" in _all_text(root),
                "conversion progress was not displayed",
            )
            assert worker_threads == [worker_threads[0]] and worker_threads[0] != timer.main_thread
            screenshots.append(_capture(root, "05-converting.png"))

            release_install.set()
            _wait_for(
                root,
                timer,
                lambda: any(kind == "showinfo" for kind, _message in dialogs.calls),
                "completion message was not shown",
            )
            done_words = _all_text(root) + "\n" + "\n".join(message for _kind, message in dialogs.calls)
            assert "Done" in done_words
            assert backup.name in done_words, "completion did not name the replaced-packs backup folder"
            assert "old packs were moved" not in done_words.casefold(), done_words
            assert (game / "mods" / "Unrelated Pack.o2r").read_bytes() == b"unrelated pack"
            assert not (game / "mods" / "Djipi's 3DE - Old Textures.o2r").exists()
            assert (backup / "Djipi's 3DE - Old Textures.o2r").read_bytes() == b"old Djipi pack"
            assert {path.name for path in backup.iterdir()} == {"Djipi's 3DE - Old Textures.o2r"}
            button_texts = [
                _widget_text(widget, root)
                for widget in _walk(root)
                if widget.winfo_class() in {"TButton", "Button"}
            ]
            assert button_texts.count("Close") == 1
            assert "Cancel" not in button_texts and "Start" not in button_texts
            assert str(_find_button(root, lambda text: text == "Back").cget("state")) == "disabled"
            screenshots.append(_capture(root, "06-done.png"))

            timer.call(
                "show synthetic error",
                lambda: window._finished(error=packhelper.HelperFailure("The synthetic pack could not be read.")),
            )
            _pump(root, timer)
            assert dialogs.error_window is not None
            screenshots.append(_capture(root, "07-error.png", dialogs.error_window))
            close_button = _find_button(root, lambda text: text == "Close")
            timer.call("Close", close_button.invoke)
            timer.assert_fast()
        finally:
            release_install.set()
            packhelper.check_free_space = original_check_free_space
            packhelper.select_packs = original_select_packs
            packhelper.run_install = original_run_install
            dialogs.restore()
            try:
                root.destroy()
            except tk.TclError:
                pass

    return {
        "screenshots": [str(path.relative_to(REPOSITORY_ROOT)) for path in screenshots],
        "max_tk_call_seconds": round(timer.maximum, 6),
        "tk_call_limit_seconds": TK_LIMIT_SECONDS,
    }


def main() -> int:
    try:
        summary = run()
    except BaseException as error:
        summary = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        print(json.dumps(summary, indent=2), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
