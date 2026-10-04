#!/usr/bin/env python3
"""A small three-step GUI and CLI for preparing SoH 9.2.3 Wii U texture packs."""

from __future__ import annotations

import argparse
import datetime as _datetime
import errno
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


try:
    from soh_wiiu_packtool import (  # type: ignore[import-not-found]
        PackSource,
        archive_members,
        bounded_jobs,
        build_bc_members,
        build_blank_skyboxes,
        build_lookup_index,
        collect_sources,
        profile_sort_key,
        resource_headers,
        source_pack_name,
        transform_members,
        write_archive,
        write_load_order,
    )
except ModuleNotFoundError:
    # Running the file directly is useful on a checkout, while PyInstaller
    # imports soh_wiiu_packtool as a bundled top-level module.
    _tools_dir = Path(__file__).resolve().parent
    if str(_tools_dir) not in sys.path:
        sys.path.insert(0, str(_tools_dir))
    from soh_wiiu_packtool import (  # type: ignore[import-not-found]
        PackSource,
        archive_members,
        bounded_jobs,
        build_bc_members,
        build_blank_skyboxes,
        build_lookup_index,
        collect_sources,
        profile_sort_key,
        resource_headers,
        source_pack_name,
        transform_members,
        write_archive,
        write_load_order,
    )


EXPECTED_VERSION = (9, 2, 3)
PACK_SUFFIXES = (".zip", ".otr", ".o2r")
MODS_PLACEHOLDER = "PUT-TEXTURE-PACKS-HERE.txt"
# Where the tested packs are downloaded (both are files on this one page). Never bundled with the helper.
PACK_DOWNLOADS = (
    ("Djipi's 3DS Experience + Skilar's Art Plus Link (GameBanana)", "https://gamebanana.com/mods/477979"),
)
DONE_MESSAGE = "Done - put the SD card back in your Wii U and start Ship of Harkinian"

SD_NOT_FOUND = "No Ship of Harkinian here: choose the SD card that has wiiu/apps/soh923 with oot.o2r in it"
SOH_NOT_INSTALLED = (
    "Ship of Harkinian is not installed on this SD card yet. Install it first "
    "(see the README), then choose the SD card folder."
)
OOT_MISSING = (
    "Ship of Harkinian is installed, but oot.o2r is missing or has no version marker. "
    "Copy the SoH 9.2.3 Wii U oot.o2r to wiiu/apps/soh923/ on the SD card, then choose the folder again."
)
OOT_WRONG_VERSION = (
    "This oot.o2r is from another SoH version; use the SoH 9.2.3 Wii U oot.o2r, then choose the folder again."
)
CLI_SD_ROOT_REQUIRED = (
    "CLI mode needs an SD card path; pass --sd-root PATH with the card root or a folder containing "
    "wiiu/apps/soh923."
)
NOT_ENOUGH_SPACE = (
    "There is not enough free space; use a larger SD card or free some space, then try again."
)
UNRECOGNISED_FILE = (
    "That file is not a recognised SoH pack; choose a .zip, .otr, or .o2r download and try again."
)
OUT_OF_MEMORY = "The computer ran out of memory; close other programs and try again with fewer packs."
CANCELLED = "The conversion was cancelled; press Start again when you are ready."
INSTALL_FAILED = "The pack could not be installed; check that the SD card is writable and try again."
WINDOW_FAILED = "The helper window could not open; run this program with --cli instead."


class HelperFailure(Exception):
    """An error whose message is safe to show directly to a novice."""


class Cancelled(HelperFailure):
    def __init__(self) -> None:
        super().__init__(CANCELLED)


class OutOfSpace(HelperFailure):
    def __init__(self) -> None:
        super().__init__(NOT_ENOUGH_SPACE)


class UnrecognisedPack(HelperFailure):
    def __init__(self) -> None:
        super().__init__(UNRECOGNISED_FILE)


class SdCardNotFound(HelperFailure):
    def __init__(self) -> None:
        super().__init__(SD_NOT_FOUND)


class SohNotInstalled(HelperFailure):
    def __init__(self) -> None:
        super().__init__(SOH_NOT_INSTALLED)


class OotMissing(HelperFailure):
    def __init__(self) -> None:
        super().__init__(OOT_MISSING)


class OotWrongVersion(HelperFailure):
    def __init__(self) -> None:
        super().__init__(OOT_WRONG_VERSION)


Progress = Callable[[int, str], None]


@dataclass(frozen=True)
class SdCard:
    root: Path
    game_dir: Path


@dataclass(frozen=True)
class PackSelection:
    path: Path
    label: str
    sources: tuple[PackSource, ...]


@dataclass(frozen=True)
class PackHint:
    """The cheap-to-compute information shown before conversion starts."""

    path: Path
    label: str


@dataclass(frozen=True)
class InstallResult:
    card: SdCard
    backup: Path | None
    output: Path
    pack_labels: tuple[str, ...]


def _game_dir_for(path: Path) -> Path | None:
    """Return the SoH directory if *path* is an SD root or a picked subfolder."""
    path = path.expanduser()
    checks = (
        (path, path / "wiiu" / "apps" / "soh923"),
        (path.parent, path / "apps" / "soh923"),
        (path.parent.parent.parent, path),
    )
    for root, game_dir in checks:
        try:
            if game_dir.is_dir() and game_dir.name.casefold() == "soh923":
                return game_dir
        except OSError:
            continue
    return None


def _wiiu_root_for(path: Path) -> Path | None:
    """Return the selected card root when *path* has a Wii U directory."""
    path = path.expanduser()
    checks = ((path, path / "wiiu"), (path.parent, path))
    for root, wiiu_dir in checks:
        try:
            if wiiu_dir.is_dir() and wiiu_dir.name.casefold() == "wiiu":
                return root
        except OSError:
            continue
    return None


def _card_for(path: Path) -> SdCard | None:
    game_dir = _game_dir_for(path)
    if game_dir is None:
        return None
    try:
        relative = game_dir.relative_to(path)
    except ValueError:
        # The selected folder itself may be wiiu/apps/soh923.
        if path.name.casefold() == "soh923":
            root = path.parent.parent.parent
        else:
            return None
    else:
        root = path
        if not relative.parts and path.name.casefold() == "soh923":
            root = path.parent.parent.parent
        elif relative.parts == ("apps", "soh923"):
            root = path.parent
        elif relative.parts == ("wiiu", "apps", "soh923"):
            root = path
    return SdCard(root=root, game_dir=game_dir)


def locate_sd_card(root: Path) -> SdCard:
    """Validate the folder explicitly chosen by the user."""
    selected = Path(root).expanduser()
    card = _card_for(selected)
    if card is None:
        if _wiiu_root_for(selected) is not None:
            raise SohNotInstalled()
        raise SdCardNotFound()
    return card


def _version_from_bytes(data: bytes) -> tuple[int, int, int] | None:
    if len(data) >= 7 and data[0] in (0, 1):
        order = ">" if data[0] == 1 else "<"
        return struct.unpack_from(order + "HHH", data, 1)
    match = re.search(rb"(\d+)\.(\d+)\.(\d+)", data)
    if match:
        return tuple(int(part) for part in match.groups())  # type: ignore[return-value]
    return None


def read_port_version(oot: Path) -> tuple[int, int, int]:
    """Read SoH's endian-tagged ``portVersion`` member from an O2R ZIP."""
    try:
        with zipfile.ZipFile(oot) as archive:
            member = next(
                (info for info in archive.infolist() if not info.is_dir() and info.filename == "portVersion"),
                None,
            )
            if member is None:
                raise KeyError("portVersion")
            version = _version_from_bytes(archive.read(member))
    except (OSError, KeyError, zipfile.BadZipFile, RuntimeError) as error:
        raise OotMissing() from error
    if version is None:
        raise OotMissing()
    return version


def validate_oot(oot: Path) -> tuple[int, int, int]:
    if not oot.is_file():
        raise OotMissing()
    try:
        version = read_port_version(oot)
    except OotMissing:
        raise
    except (OSError, zipfile.BadZipFile, ValueError) as error:
        raise OotMissing() from error
    if version != EXPECTED_VERSION:
        raise OotWrongVersion()
    return version


def friendly_pack_name(path: Path, sources: Iterable[PackSource] = ()) -> str:
    """Use the two familiar author names in the UI without renaming files."""
    haystack = " ".join((path.name, *(source.origin for source in sources))).casefold()
    if "skilar" in haystack or "art plus" in haystack or "artplus" in haystack:
        return "Skilar's Art Plus Link"
    if "djipi" in haystack or "3de" in haystack or "3ds experience" in haystack:
        return "Djipi's 3DS Experience"
    return path.name


def _zip_member_names(path: Path) -> tuple[str, ...]:
    """Read only a ZIP central directory; never decompress a pack here."""
    try:
        with zipfile.ZipFile(path) as archive:
            return tuple(info.filename for info in archive.infolist() if not info.is_dir())
    except (OSError, ValueError, zipfile.BadZipFile):
        # A file with a pack suffix is allowed into the list and will receive
        # the useful, detailed error from the worker when Start is pressed.
        return ()


def _friendly_pack_name_from_names(path: Path, names: Iterable[str]) -> str:
    haystack = " ".join((path.name, *names)).casefold()
    if "skilar" in haystack or "art plus" in haystack or "artplus" in haystack:
        return "Skilar's Art Plus Link"
    if "djipi" in haystack or "3de" in haystack or "3ds experience" in haystack:
        return "Djipi's 3DS Experience"
    return path.name


def identify_packs(paths: Iterable[Path]) -> list[PackHint]:
    """Identify packs without opening their contents.

    This is deliberately suitable for the Tk thread: it checks the filename
    and, for a download ZIP, its central-directory member names only. Full
    archive parsing is left to ``select_packs`` in the conversion worker.
    """
    hints: list[PackHint] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        key = os.path.normcase(os.path.abspath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        if not path.is_file() or path.suffix.casefold() not in PACK_SUFFIXES:
            raise UnrecognisedPack()
        names = _zip_member_names(path) if path.suffix.casefold() == ".zip" else ()
        hints.append(PackHint(path, _friendly_pack_name_from_names(path, names)))
    if not hints:
        raise UnrecognisedPack()
    return hints


def select_packs(paths: Iterable[Path]) -> list[PackSelection]:
    selections: list[PackSelection] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        key = os.path.normcase(os.path.abspath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        if not path.is_file() or path.suffix.casefold() not in PACK_SUFFIXES:
            raise UnrecognisedPack()
        try:
            sources = tuple(collect_sources(path))
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
            raise UnrecognisedPack() from error
        if not sources or any(not source.members for source in sources):
            raise UnrecognisedPack()
        selections.append(PackSelection(path, friendly_pack_name(path, sources), sources))
    if not selections:
        raise UnrecognisedPack()
    return selections


def estimate_required_space(paths: Iterable[Path]) -> int:
    """Give the preflight check room for converted archives and the SD copy."""
    total = 0
    try:
        for path in paths:
            total += max(0, Path(path).stat().st_size)
    except OSError as error:
        raise UnrecognisedPack() from error
    return max(1024 * 1024, total * 2 + 16 * 1024 * 1024)


def _same_device(first: Path, second: Path) -> bool:
    try:
        return os.stat(first).st_dev == os.stat(second).st_dev
    except OSError:
        return False


def check_free_space(
    card_root: Path,
    temp_parent: Path,
    required: int,
    disk_usage: Callable[[str | os.PathLike[str]], object] = shutil.disk_usage,
) -> None:
    try:
        card_free = int(getattr(disk_usage(card_root), "free"))
        temp_free = int(getattr(disk_usage(temp_parent), "free"))
    except (OSError, TypeError, ValueError, AttributeError) as error:
        raise OutOfSpace() from error
    card_required = required * 2 if _same_device(card_root, temp_parent) else required
    if card_free < card_required or temp_free < required:
        raise OutOfSpace()


def _is_cancelled(cancel_event: threading.Event | Callable[[], bool] | None) -> bool:
    if cancel_event is None:
        return False
    if callable(cancel_event):
        return bool(cancel_event())
    return cancel_event.is_set()


def _check_cancel(cancel_event: threading.Event | Callable[[], bool] | None) -> None:
    if _is_cancelled(cancel_event):
        raise Cancelled()


def _report(progress: Progress | None, percent: int, message: str) -> None:
    if progress is not None:
        progress(max(0, min(100, percent)), message)


def _convert_to_temp(
    oot: Path,
    selections: list[PackSelection],
    output: Path,
    jobs: int,
    progress: Progress | None,
    cancel_event: threading.Event | Callable[[], bool] | None,
) -> None:
    sources = [source for selection in selections for source in selection.sources]
    if not sources:
        raise UnrecognisedPack()
    _check_cancel(cancel_event)
    headers = resource_headers(oot)
    index = build_lookup_index(oot, sources)
    output.mkdir(parents=True, exist_ok=True)
    output_names: list[str] = []
    conversion_total = max(1, len(sources))
    for index_number, source in enumerate(sources, 1):
        _check_cancel(cancel_event)
        transformed, _stats = transform_members(source.members, index, headers)
        try:
            bc_members, _bc_counts = build_bc_members(transformed, jobs)
        except MemoryError:
            raise
        output_members = dict(transformed)
        output_members.update(bc_members)
        filename = f"{source_pack_name(source.origin)}.o2r"
        if filename in output_names:
            raise HelperFailure(INSTALL_FAILED)
        write_archive(output / filename, output_members)
        output_names.append(filename)
        _report(progress, 10 + int(index_number * 55 / conversion_total), f"Making pack {index_number} of {conversion_total}...")

    _check_cancel(cancel_event)
    _report(progress, 75, "Adding the files needed by the Wii U version...")

    if any("djipi" in source.origin.casefold() for source in sources):
        _check_cancel(cancel_event)
        main_pack = next(
            (source.members for source in sources if "01 main textures" in source.origin.casefold()),
            None,
        )
        if main_pack is None:
            raise HelperFailure(INSTALL_FAILED)
        skybox = build_blank_skyboxes(oot, main_pack)
        write_archive(output / "zz-fix-blank-skyboxes.o2r", skybox)
        output_names.append("zz-fix-blank-skyboxes.o2r")

    output_names.sort(key=profile_sort_key)
    write_load_order(output, output_names)
    _report(progress, 84, "Getting the new packs ready to copy...")


def backup_existing_mods(
    game_dir: Path,
    now: _datetime.datetime | None = None,
) -> Path | None:
    """Legacy helper that preserves a complete mods directory under a timestamp."""
    mods = game_dir / "mods"
    if not mods.exists() and not mods.is_symlink():
        return None
    try:
        search_root = mods.resolve() if mods.is_symlink() else mods
        if not search_root.is_dir():
            return None
        placeholder = search_root / MODS_PLACEHOLDER
        has_user_files = False
        for path in search_root.rglob("*"):
            if path == placeholder and path.is_file() and not path.is_symlink():
                continue
            if not path.is_dir() or path.is_symlink():
                has_user_files = True
                break
    except OSError:
        # If the contents cannot be checked, preserve the existing directory.
        has_user_files = True
    if not has_user_files:
        return None
    stamp = (now or _datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
    backup = game_dir / f"mods-backup-{stamp}"
    suffix = 2
    while backup.exists() or backup.is_symlink():
        backup = game_dir / f"mods-backup-{stamp}-{suffix}"
        suffix += 1
    mods.rename(backup)
    return backup


def remove_nonpack_mods(game_dir: Path) -> None:
    """Remove only the release placeholder and empty directories.

    New installs stage a merged directory instead of using this whole-folder
    cleanup path; it remains for callers that explicitly need the old helper.
    """
    mods = game_dir / "mods"
    if not mods.exists() and not mods.is_symlink():
        return
    if mods.is_dir() and not mods.is_symlink():
        placeholder = mods / MODS_PLACEHOLDER
        if placeholder.is_file() and not placeholder.is_symlink():
            placeholder.unlink()
        for path in sorted(mods.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_dir() and not path.is_symlink():
                try:
                    path.rmdir()
                except OSError:
                    pass
        try:
            mods.rmdir()
        except OSError:
            pass
    else:
        mods.unlink()


def copy_directory_with_progress(
    source: Path,
    destination: Path,
    progress: Progress | None = None,
    cancel_event: threading.Event | Callable[[], bool] | None = None,
) -> None:
    files = [path for path in source.rglob("*") if path.is_file()]
    total = sum(path.stat().st_size for path in files)
    copied = 0
    destination.mkdir(parents=True, exist_ok=False)
    for source_file in files:
        _check_cancel(cancel_event)
        target = destination / source_file.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        with source_file.open("rb") as source_stream, target.open("wb") as target_stream:
            while True:
                _check_cancel(cancel_event)
                chunk = source_stream.read(1024 * 1024)
                if not chunk:
                    break
                target_stream.write(chunk)
                copied += len(chunk)
                percent = 84 if total == 0 else 84 + int(copied * 16 / total)
                _report(progress, percent, "Copying the new packs to the SD card...")
        shutil.copystat(source_file, target, follow_symlinks=True)
    _report(progress, 100, "Finished copying the new packs.")


def _copy_directory_contents_with_progress(
    source: Path,
    destination: Path,
    progress: Progress | None,
    cancel_event: threading.Event | Callable[[], bool] | None,
    *,
    start_percent: int,
    end_percent: int,
    message: str,
    skip: Iterable[Path] = (),
) -> None:
    """Copy files into an already-created stage directory (except those in *skip*).

    The destination is deliberately a sibling staging directory.  The live
    ``mods`` path is not touched until the complete merged tree is ready.
    """
    if not source.is_dir() or source.is_symlink():
        return
    skipped = {os.path.normcase(os.path.abspath(str(path))) for path in skip}
    files = [
        path
        for path in source.rglob("*")
        if path.is_file() and os.path.normcase(os.path.abspath(str(path))) not in skipped
    ]
    total = sum(path.stat().st_size for path in files)
    copied = 0
    for source_file in files:
        _check_cancel(cancel_event)
        target = destination / source_file.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        with source_file.open("rb") as source_stream, target.open("wb") as target_stream:
            while True:
                _check_cancel(cancel_event)
                chunk = source_stream.read(1024 * 1024)
                if not chunk:
                    break
                target_stream.write(chunk)
                copied += len(chunk)
                percent = start_percent if total == 0 else start_percent + int(copied * (end_percent - start_percent) / total)
                _report(progress, percent, message)
        shutil.copystat(source_file, target, follow_symlinks=True)
    _report(progress, end_percent, message)


def _pack_family(name: str) -> str | None:
    """Return the leading family key used to replace related converted packs."""
    stem = Path(name).stem.casefold()
    if stem == "zz-fix-blank-skyboxes":
        return "djipi"
    if stem.startswith("djipi's 3de - "):
        return "djipi"
    if stem.startswith("art plus - "):
        return "art-plus"
    prefix, separator, _rest = stem.partition(" - ")
    return f"{prefix}{separator}" if separator else None


def _pack_family_for_label(label: str) -> str | None:
    """Map the friendly step-2 label to the family used by generated names."""
    lowered = label.casefold()
    if "djipi" in lowered or "3ds experience" in lowered or "3de" in lowered:
        return "djipi"
    if "skilar" in lowered or "art plus" in lowered or "artplus" in lowered:
        return "art-plus"
    return _pack_family(label)


def _pack_files(mods: Path) -> list[Path]:
    if not mods.is_dir() or mods.is_symlink():
        return []
    return [path for path in mods.iterdir() if path.is_file() and path.suffix.casefold() in PACK_SUFFIXES]


def _files_replaced_by_run(mods: Path, produced_names: set[str]) -> list[Path]:
    """Find existing pack files that the converted output supersedes."""
    if not mods.is_dir() or mods.is_symlink():
        return []
    produced_keys = {name.casefold() for name in produced_names}
    families = {_pack_family(name) for name in produced_names}
    families.discard(None)
    replaced: list[Path] = []
    for path in mods.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in PACK_SUFFIXES:
            continue
        if path.name.casefold() in produced_keys or _pack_family(path.name) in families:
            replaced.append(path)
    return replaced


def _unique_path(parent: Path, prefix: str) -> Path:
    candidate = parent / prefix
    suffix = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = parent / f"{prefix}-{suffix}"
        suffix += 1
    return candidate


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _remove_empty_parent_dirs(path: Path, stop: Path) -> None:
    parent = path.parent
    while parent != stop and parent != parent.parent:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def run_install(
    sd_root: Path,
    pack_paths: Iterable[Path],
    *,
    oot_path: Path | None = None,
    jobs: int = 1,
    temp_parent: Path | None = None,
    progress: Progress | None = None,
    cancel_event: threading.Event | Callable[[], bool] | None = None,
    disk_usage: Callable[[str | os.PathLike[str]], object] = shutil.disk_usage,
    now: _datetime.datetime | None = None,
) -> InstallResult:
    card = locate_sd_card(sd_root)
    oot = Path(oot_path) if oot_path is not None else card.game_dir / "oot.o2r"
    validate_oot(oot)
    selections = select_packs(pack_paths)
    required = estimate_required_space(selection.path for selection in selections)
    temp_base = Path(temp_parent) if temp_parent is not None else Path(tempfile.gettempdir())
    temp_base.mkdir(parents=True, exist_ok=True)
    check_free_space(card.root, temp_base, required, disk_usage)
    _check_cancel(cancel_event)
    try:
        bounded_jobs_count = bounded_jobs(jobs)
    except (TypeError, ValueError) as error:
        raise HelperFailure(INSTALL_FAILED) from error

    try:
        with tempfile.TemporaryDirectory(prefix="soh-wiiu-pack-helper-", dir=str(temp_base)) as temp_name:
            converted = Path(temp_name) / "mods"
            _convert_to_temp(oot, selections, converted, bounded_jobs_count, progress, cancel_event)
            _check_cancel(cancel_event)
            converted_size = sum(path.stat().st_size for path in converted.rglob("*") if path.is_file())
            mods = card.game_dir / "mods"
            mods_source = mods.resolve() if mods.is_symlink() else mods
            produced_names = {
                path.name
                for path in converted.iterdir()
                if path.is_file() and path.suffix.casefold() in PACK_SUFFIXES
            }
            replaced_files = _files_replaced_by_run(mods_source, produced_names)
            # The staged folder holds the converted packs plus a copy of every pack that stays (the live mods/ is
            # only swapped once that copy is complete), so both must fit on the card at the same time.
            replaced_keys = {os.path.normcase(os.path.abspath(str(path))) for path in replaced_files}
            kept_size = sum(
                path.stat().st_size
                for path in (mods_source.rglob("*") if mods_source.is_dir() else ())
                if path.is_file() and os.path.normcase(os.path.abspath(str(path))) not in replaced_keys
            )
            try:
                if int(getattr(disk_usage(card.root), "free")) < converted_size + kept_size:
                    raise OutOfSpace()
            except (OSError, TypeError, ValueError, AttributeError) as error:
                raise OutOfSpace() from error
            stamp = (now or _datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
            stage = _unique_path(card.game_dir, f".mods-install-{stamp}")
            stage.mkdir()
            installed = False
            backup: Path | None = None
            previous = _unique_path(card.game_dir, f".mods-previous-{stamp}")
            try:
                _copy_directory_contents_with_progress(
                    mods_source,
                    stage,
                    progress,
                    cancel_event,
                    start_percent=84,
                    end_percent=91,
                    message="Preparing the existing packs to stay in place...",
                    skip=replaced_files,
                )
                for old_file in replaced_files:
                    relative = old_file.relative_to(mods_source)
                    _remove_path(stage / relative)
                _copy_directory_contents_with_progress(
                    converted,
                    stage,
                    progress,
                    cancel_event,
                    start_percent=91,
                    end_percent=99,
                    message="Copying the converted packs into the staged mods folder...",
                )
                placeholder = stage / MODS_PLACEHOLDER
                if placeholder.is_file() and not placeholder.is_symlink():
                    placeholder.unlink()
                pack_names = sorted(
                    (
                        path.name
                        for path in stage.iterdir()
                        if path.is_file() and path.suffix.casefold() in PACK_SUFFIXES
                    ),
                    key=profile_sort_key,
                )
                write_load_order(stage, pack_names)
                _check_cancel(cancel_event)

                if replaced_files:
                    backup = _unique_path(card.game_dir, f"mods-backup-{stamp}")
                    backup.mkdir()
                if mods.exists() or mods.is_symlink():
                    mods.rename(previous)
                try:
                    stage.rename(mods)
                except BaseException:
                    if previous.exists() or previous.is_symlink():
                        previous.rename(mods)
                    raise
                installed = True

                if backup is not None:
                    for old_file in replaced_files:
                        relative = old_file.relative_to(mods_source)
                        source = previous / relative
                        target = backup / relative
                        if source.exists() or source.is_symlink():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(source), str(target))
                            _remove_empty_parent_dirs(source, previous)
                _remove_path(previous)
                _report(progress, 100, "Finished merging the packs into mods/.")
                return InstallResult(card, backup, mods, tuple(selection.label for selection in selections))
            finally:
                if not installed:
                    _remove_path(stage)
                    if backup is not None and backup.exists() and not any(backup.iterdir()):
                        backup.rmdir()
    except MemoryError:
        raise
    except Cancelled:
        raise
    except HelperFailure:
        raise
    except OSError as error:
        if error.errno == errno.ENOSPC:
            raise OutOfSpace() from error
        raise HelperFailure(INSTALL_FAILED) from error
    except (ValueError, RuntimeError, zipfile.BadZipFile) as error:
        raise HelperFailure(INSTALL_FAILED) from error


def user_message(error: BaseException) -> str:
    if isinstance(error, HelperFailure):
        return str(error)
    if isinstance(error, MemoryError):
        return OUT_OF_MEMORY
    if isinstance(error, KeyboardInterrupt):
        return CANCELLED
    return INSTALL_FAILED


def installation_done_message(result: InstallResult) -> str:
    """Describe the completed install in terms a first-time user can use."""
    if result.backup is None:
        backup_note = "No existing pack files were replaced; your other packs stayed in mods/."
    else:
        backup_note = f"Replaced pack files were moved to {result.backup.name}; your other packs stayed in mods/."
    return f"{DONE_MESSAGE}. {backup_note}"


def _selftest_archive(path: Path, *, version: tuple[int, int, int] | None = EXPECTED_VERSION) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if version is not None:
            archive.writestr("portVersion", b"\x01" + struct.pack(">HHH", *version))
        archive.writestr("root/resource", b"\x00\x00\x00\x00TLDO" + b"\x00" * 56)


def run_selftest() -> int:
    with tempfile.TemporaryDirectory(prefix="soh-wiiu-pack-helper-selftest-") as name:
        root = Path(name)
        game = root / "wiiu" / "apps" / "soh923"
        game.mkdir(parents=True)
        _selftest_archive(game / "oot.o2r")
        pack = root / "Art Plus - New.o2r"
        _selftest_archive(pack, version=None)
        with zipfile.ZipFile(pack, "a") as archive:
            archive.writestr("alt/synthetic", b"synthetic test data")
        old_mods = game / "mods"
        old_mods.mkdir()
        (old_mods / "keep-me.txt").write_text("not deleted")
        (old_mods / "Art Plus - Old.o2r").write_bytes(b"old family pack")
        (old_mods / "unrelated.o2r").write_bytes(b"unrelated pack")
        result = run_install(root, [pack], jobs=1)
        assert result.output.is_dir()
        with zipfile.ZipFile(result.output / "Art Plus - New.o2r") as archive:
            assert archive.read("alt/synthetic") == b"synthetic test data"
        assert result.backup is not None
        assert (result.backup / "Art Plus - Old.o2r").read_bytes() == b"old family pack"
        assert (result.output / "keep-me.txt").read_text() == "not deleted"
        assert (result.output / "unrelated.o2r").read_bytes() == b"unrelated pack"
        assert not (result.output / "Art Plus - Old.o2r").exists()
        assert friendly_pack_name(pack) == "Skilar's Art Plus Link"

        other_pack = root / "Other Pack.o2r"
        _selftest_archive(other_pack, version=None)
        second = run_install(root, [other_pack], jobs=1)
        assert second.backup is None
        assert (second.output / "Art Plus - New.o2r").is_file()
        assert (second.output / "Other Pack.o2r").is_file()
        assert (second.output / "unrelated.o2r").read_bytes() == b"unrelated pack"
        assert "Art Plus - New.o2r" in (second.output / "LOAD_ORDER.txt").read_text()
        assert "Other Pack.o2r" in (second.output / "LOAD_ORDER.txt").read_text()
    print("Self-test passed.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", action="store_true", help="run without opening a window")
    parser.add_argument("--selftest", action="store_true", help="run the synthetic end-to-end self-test")
    parser.add_argument("--sd-root", type=Path, help="SD card root, useful for scripts and tests")
    parser.add_argument("--oot", type=Path, help="override the oot.o2r path")
    parser.add_argument("--packs", nargs="+", type=Path, dest="pack_options", help="pack files to install")
    parser.add_argument("--jobs", type=int, default=1, help="BC conversion workers (1 is safest for memory)")
    parser.add_argument("--temp-parent", type=Path, help="temporary workspace parent")
    parser.add_argument("paths", nargs="*", type=Path, help="pack files, including files dropped on the program")
    return parser


def _run_cli(args: argparse.Namespace) -> int:
    paths = [*(args.pack_options or ()), *args.paths]
    if args.sd_root is None:
        print(CLI_SD_ROOT_REQUIRED, file=sys.stderr)
        return 2
    try:
        result = run_install(
            args.sd_root,
            paths,
            oot_path=args.oot,
            jobs=args.jobs,
            temp_parent=args.temp_parent,
        )
    except BaseException as error:
        if isinstance(error, (SystemExit, GeneratorExit)):
            raise
        print(user_message(error), file=sys.stderr)
        return 1
    for label in result.pack_labels:
        print(f"Prepared {label}.")
    print(installation_done_message(result))
    return 0


class PackHelperWindow:
    def __init__(self, root: object, initial_paths: list[Path], explicit_oot: Path | None = None):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk = tk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.ttk = ttk
        self.root = root
        self.initial_paths = initial_paths
        self.explicit_oot = explicit_oot
        self.card: SdCard | None = None
        self.oot: Path | None = None
        self.pack_paths: list[Path] = []
        self.pack_labels: dict[str, str] = {}
        self.cancel_event = threading.Event()
        self._card_validation_running = False
        self.step = 1
        self.status = tk.StringVar(value="Choose your SD card folder to continue.")
        self.progress_value = tk.IntVar(value=0)
        self.progress_text = tk.StringVar(value="")
        self.required_space = 0
        root.title("SoH Wii U Pack Helper")
        root.minsize(600, 360)
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        self._show_step_one()

    def _error(self, error: BaseException | str) -> None:
        self.messagebox.showerror("SoH Wii U Pack Helper", user_message(error) if isinstance(error, BaseException) else error)

    def _clear(self) -> None:
        for child in self.root.winfo_children():
            child.destroy()

    def _header(self, number: int, title: str) -> None:
        self.ttk.Label(self.root, text=f"Step {number} of 3: {title}", font=("TkDefaultFont", 15, "bold")).pack(
            anchor="w", padx=24, pady=(22, 8)
        )

    @staticmethod
    def _pack_key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(str(path)))

    def _start_card_validation(self, selected: Path) -> None:
        if self._card_validation_running:
            return
        self.card = None
        self.oot = None
        self._card_validation_running = True
        self.status.set("Checking the chosen folder...")
        self._show_step_one()
        threading.Thread(target=self._card_validation_worker, args=(selected,), daemon=True).start()

    def _card_validation_worker(self, selected: Path) -> None:
        try:
            card = locate_sd_card(selected)
            oot = self.explicit_oot or card.game_dir / "oot.o2r"
            validate_oot(oot)
        except BaseException as error:
            self.root.after(0, lambda error=error: self._card_validation_finished(error=error))
        else:
            self.root.after(0, lambda: self._card_validation_finished(card=card, oot=oot))

    def _card_validation_finished(
        self,
        *,
        card: SdCard | None = None,
        oot: Path | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._card_validation_running = False
        if error is not None:
            self.card = None
            self.oot = None
            self.status.set(user_message(error))
            self._show_step_one()
            return

        assert card is not None and oot is not None
        self.card = card
        self.oot = oot
        dropped_paths = self.initial_paths
        self.initial_paths = []
        self.status.set("SD card found. Your SoH 9.2.3 files are ready.")
        self._show_step_one()
        if dropped_paths:
            self._add_packs(dropped_paths)

    def _set_card(self, selected: Path, show_errors: bool = True) -> None:
        """Start validation without blocking Tk; ``show_errors`` is retained for callers."""
        del show_errors
        self._start_card_validation(Path(selected))

    def _choose_sd(self) -> None:
        selected = self.filedialog.askdirectory(title="Choose your SD card...")
        if selected:
            self._set_card(Path(selected))
        else:
            self.status.set("Choose your SD card folder to continue.")
            self._show_step_one()

    def _show_step_one(self) -> None:
        self.step = 1
        self._clear()
        self._header(1, "Choose your SD card")
        self.ttk.Label(
            self.root,
            text=(
                "Choose the SD card's top folder (for example, E:\\), or choose a folder that contains "
                "wiiu/apps/soh923. The folder must contain oot.o2r too. Using USB Partition instead of an SD "
                "card? Choose the FAT32 part of the USB drive. If Windows asks to format a drive, click Cancel."
            ),
            wraplength=540,
        ).pack(anchor="w", padx=24, pady=8)
        self.ttk.Label(self.root, textvariable=self.status, wraplength=540).pack(anchor="w", padx=24, pady=12)
        buttons = self.ttk.Frame(self.root)
        buttons.pack(side="bottom", fill="x", padx=24, pady=22)
        self.ttk.Button(buttons, text="Choose your SD card...", command=self._choose_sd).pack(side="left")
        self.ttk.Button(buttons, text="Next", command=self._show_step_two, state="normal" if self.card else "disabled").pack(
            side="right"
        )

    def _add_packs(self, paths: Iterable[Path]) -> None:
        try:
            for hint in identify_packs(paths):
                key = self._pack_key(hint.path)
                if key not in {self._pack_key(path) for path in self.pack_paths}:
                    self.pack_paths.append(hint.path)
                    self.pack_labels[key] = hint.label
        except BaseException as error:
            self._error(error)
        self._show_step_two()

    def _choose_packs(self) -> None:
        paths = self.filedialog.askopenfilenames(
            title="Choose downloaded SoH texture packs",
            filetypes=(("SoH packs", "*.zip *.otr *.o2r"), ("All files", "*.*")),
        )
        if paths:
            self._add_packs(Path(path) for path in paths)

    def _show_step_two(self) -> None:
        self.step = 2
        self._clear()
        self._header(2, "Choose the downloaded packs")
        self.ttk.Label(
            self.root,
            text="Choose downloaded .zip, .otr, or .o2r files. The helper will check them fully after you press Start.",
            wraplength=540,
        ).pack(anchor="w", padx=24, pady=8)
        self.ttk.Label(self.root, text="No packs yet? Download them from the pack's own page:", wraplength=540).pack(
            anchor="w", padx=24
        )
        for title, url in PACK_DOWNLOADS:
            link = self.tk.Label(self.root, text=title, fg="#1a5fb4", cursor="hand2", font=("TkDefaultFont", 10, "underline"))
            link.pack(anchor="w", padx=36, pady=2)
            link.bind("<Button-1>", lambda _event, url=url: self._open_link(url))
        listbox = self.tk.Listbox(self.root, height=8)
        listbox.pack(fill="both", expand=True, padx=24, pady=8)
        for path in self.pack_paths:
            label = self.pack_labels.get(self._pack_key(path), path.name)
            listbox.insert("end", f"{label}  ({path.name})")
        buttons = self.ttk.Frame(self.root)
        buttons.pack(side="bottom", fill="x", padx=24, pady=22)
        self.ttk.Button(buttons, text="Back", command=self._show_step_one).pack(side="left")
        self.ttk.Button(buttons, text="Add pack files...", command=self._choose_packs).pack(side="left", padx=8)
        self.ttk.Button(
            buttons,
            text="Next",
            command=self._show_step_three,
            state="normal" if self.pack_paths else "disabled",
        ).pack(side="right")

    def _open_link(self, url: str) -> None:
        import webbrowser

        try:
            webbrowser.open(url)
        except Exception:
            self.messagebox.showinfo("SoH Wii U Pack Helper", f"Open this page in your web browser:\n{url}")

    def _change_summary(self) -> str:
        """Describe the merge in plain words before the user presses Start."""
        assert self.card is not None
        mods = self.card.game_dir / "mods"
        existing = _pack_files(mods)
        produced_names = {path.name.casefold() for path in self.pack_paths}
        families = {
            _pack_family_for_label(self.pack_labels.get(self._pack_key(path), path.name))
            for path in self.pack_paths
        }
        families.discard(None)
        replaced = {
            path.name
            for path in existing
            if path.name.casefold() in produced_names or _pack_family(path.name) in families
        }
        labels = [self.pack_labels.get(self._pack_key(path), path.name) for path in self.pack_paths]
        changes: list[str] = []
        for path, label in zip(self.pack_paths, labels):
            family = _pack_family_for_label(label)
            matching = any(
                existing_path.name.casefold() == path.name.casefold() or _pack_family(existing_path.name) == family
                for existing_path in existing
            )
            changes.append(f"{label} will be {'updated' if matching else 'added'}.")
        change = " ".join(changes)
        other_count = max(0, len(existing) - len(replaced))
        if other_count == 1:
            return f"{change} Your 1 other pack stays as it is."
        return f"{change} Your {other_count} other packs stay as they are."

    def _show_step_three(self) -> None:
        if not self.card or not self.oot:
            self._show_step_one()
            return
        if not self.pack_paths:
            self._error(UnrecognisedPack())
            return
        self.step = 3
        self._clear()
        self._header(3, "Put the packs on the SD card")
        required = estimate_required_space(self.pack_paths)
        self.required_space = required
        self.ttk.Label(
            self.root,
            text=(
                "This usually takes about 5 to 10 minutes. Keep the SD card in this computer until the helper says Done. "
                f"It needs about {required / (1024 ** 3):.1f} GB free on this computer and on the SD card, "
                "and will merge the result into mods/. Matching old pack files go to a dated backup folder; other packs stay in place."
            ),
            wraplength=540,
        ).pack(anchor="w", padx=24, pady=10)
        self.ttk.Label(
            self.root,
            text=self._change_summary(),
            wraplength=540,
            font=("TkDefaultFont", 11, "bold"),
        ).pack(anchor="w", padx=24, pady=(0, 8))
        self.progress = self.ttk.Progressbar(self.root, variable=self.progress_value, maximum=100)
        self.progress.pack(fill="x", padx=24, pady=(22, 4))
        self.ttk.Label(self.root, textvariable=self.progress_text, wraplength=540).pack(anchor="w", padx=24, pady=4)
        self.progress_text.set("Ready to start.")
        buttons = self.ttk.Frame(self.root)
        buttons.pack(side="bottom", fill="x", padx=24, pady=22)
        self.start_button = self.ttk.Button(buttons, text="Start", command=self._start)
        self.start_button.pack(side="right")
        self.cancel_button = self.ttk.Button(buttons, text="Cancel", command=self._cancel)
        self.cancel_button.pack(side="right", padx=8)
        self.back_button = self.ttk.Button(buttons, text="Back", command=self._show_step_two)
        self.back_button.pack(side="left")

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.progress_text.set("Cancelling safely...")

    def _update_progress(self, percent: int, message: str) -> None:
        self.root.after(0, lambda: (self.progress_value.set(percent), self.progress_text.set(message)))

    def _start(self) -> None:
        assert self.card is not None
        try:
            check_free_space(self.card.root, Path(tempfile.gettempdir()), self.required_space)
        except OutOfSpace as error:
            message = user_message(error)
            self.progress_text.set(message)
            self._error(message)
            return
        self.start_button.configure(state="disabled")
        self.cancel_event.clear()
        self.progress_text.set("Checking the SD card and downloaded packs...")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        try:
            result = run_install(
                self.card.root,
                self.pack_paths,
                oot_path=self.oot,
                progress=self._update_progress,
                cancel_event=self.cancel_event,
            )
        except BaseException as error:
            self.root.after(0, lambda error=error: self._finished(error=error))
        else:
            self.root.after(0, lambda: self._finished(result=result))

    def _finished(self, result: InstallResult | None = None, error: BaseException | None = None) -> None:
        if error is not None:
            message = user_message(error)
            self.progress_text.set(message)
            self._error(message)
            if self.step == 3:
                if self.start_button.winfo_exists():
                    self.start_button.configure(state="normal")
                if self.cancel_button.winfo_exists() and self.cancel_button.cget("text") == "Cancel":
                    self.cancel_button.configure(state="normal")
            return
        assert result is not None
        self.progress_value.set(100)
        message = installation_done_message(result)
        self.progress_text.set(message)
        self.start_button.destroy()
        self.back_button.configure(state="disabled")
        self.cancel_button.configure(text="Close", command=self.root.destroy, state="normal")
        self.messagebox.showinfo("SoH Wii U Pack Helper", message)


def _launch_gui(initial_paths: list[Path], explicit_oot: Path | None) -> int:
    try:
        import tkinter as tk

        root = tk.Tk()
        PackHelperWindow(root, initial_paths, explicit_oot=explicit_oot)
        root.mainloop()
        return 0
    except BaseException as error:
        print(WINDOW_FAILED if not isinstance(error, HelperFailure) else user_message(error), file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.selftest:
        try:
            return run_selftest()
        except BaseException as error:
            print(user_message(error), file=sys.stderr)
            return 1
    if args.cli:
        return _run_cli(args)
    return _launch_gui(list(args.pack_options or ()) + list(args.paths), args.oot)


if __name__ == "__main__":
    raise SystemExit(main())
