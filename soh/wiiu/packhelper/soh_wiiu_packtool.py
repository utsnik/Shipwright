#!/usr/bin/env python3
"""Build a Wii U SoH 9.2.3 mods directory from downloaded texture packs.

The input packs remain the source of truth.  This tool only rewrites the
archives in the requested load order; it never needs a console or a ROM.
"""

from __future__ import annotations

import argparse
import bz2
import io
import os
import re
import struct
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import o2r_bc_convert as bc  # noqa: E402
import o2r_downscale as downscale  # noqa: E402
import o2r_xml_to_binary as xml_to_binary  # noqa: E402
from soh_fix_blank_skyboxes import PAIRS, palette, texture  # noqa: E402
from PIL import Image  # noqa: E402


# The measured peak was about 2.6 GB RSS with four BC workers.  Use that
# observation to cap parallelism, but always permit one worker on small hosts.
MEMORY_PER_WORKER = (26 * 1024**3 + 39) // 40
MPQ_MAGIC = b"MPQ\x1a"
O2R_SUFFIXES = (".o2r", ".otr")
_MPQ_CRYPT_TABLE: list[int] | None = None


def available_memory() -> int | None:
    """Return host memory available to new processes, when the OS reports it."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None


def bounded_jobs(requested: int) -> int:
    if requested < 1:
        raise ValueError("--jobs must be at least 1")
    free = available_memory()
    if free is None:
        return 1
    return min(requested, max(1, free // MEMORY_PER_WORKER))


def natural_key(value: str) -> tuple[object, ...]:
    return tuple(int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value))


def source_pack_name(origin: str) -> str:
    """Return the source basename with only FAT32-invalid characters replaced."""
    value = origin.rsplit(":", 1)[-1].replace("\\", "/").rsplit("/", 1)[-1]
    if value.casefold().endswith(O2R_SUFFIXES):
        value = value.rsplit(".", 1)[0]
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value)
    value = value.rstrip(" .")
    return value or "pack"


def profile_sort_key(filename: str) -> tuple[int, str]:
    """Mirror WiiUModProfile::SortNew for generated archive names."""
    name = Path(filename).stem
    if name.startswith("Art Plus - "):
        rank = 0
    elif name.startswith("Djipi's 3DE - "):
        rank = 1
    elif name.startswith("zz-"):
        rank = 3
    else:
        rank = 2
    return rank, name


def _mpq_crypt_table() -> list[int]:
    global _MPQ_CRYPT_TABLE
    if _MPQ_CRYPT_TABLE is None:
        crypt = [0] * 0x500
        seed = 0x00100001
        for index in range(0x100):
            for row in range(5):
                seed = (seed * 125 + 3) % 0x2AAAAB
                first = (seed & 0xFFFF) << 16
                seed = (seed * 125 + 3) % 0x2AAAAB
                crypt[index + row * 0x100] = first | (seed & 0xFFFF)
        _MPQ_CRYPT_TABLE = crypt
    return _MPQ_CRYPT_TABLE


def _decrypt_mpq_block(data: bytes, key: int) -> bytes:
    """Decrypt an MPQ table or sector using StormLib's legacy algorithm."""
    crypt = _mpq_crypt_table()

    result = bytearray(data)
    seed1 = key & 0xFFFFFFFF
    seed2 = 0xEEEEEEEE
    for offset in range(0, len(result) - 3, 4):
        value = int.from_bytes(result[offset : offset + 4], "little")
        seed2 = (seed2 + crypt[0x400 + (seed1 & 0xFF)]) & 0xFFFFFFFF
        value ^= (seed1 + seed2) & 0xFFFFFFFF
        result[offset : offset + 4] = value.to_bytes(4, "little")
        seed1 = (((~seed1 << 21) + 0x11111111) & 0xFFFFFFFF) | (seed1 >> 11)
        seed2 = (value + seed2 + (seed2 << 5) + 3) & 0xFFFFFFFF
    return bytes(result)


class MpqArchive:
    """Small read-only MPQ reader for the legacy `.otr` packs in the wild.

    SoH's old OTR files use ordinary MPQ v1 tables, zlib-compressed sectors,
    and the standard `(listfile)` entry.  Unsupported MPQ compression methods
    fail explicitly instead of silently emitting a partial pack.
    """

    def __init__(self, data: bytes):
        if not data.startswith(MPQ_MAGIC) or len(data) < 44:
            raise ValueError("not an MPQ v1 archive")
        (
            header_size,
            archive_size,
            format_version,
            sector_shift,
            hash_pos,
            block_pos,
            hash_count,
            block_count,
        ) = struct.unpack_from("<IIHHIIII", data, 4)
        if format_version != 1 or header_size < 32:
            raise ValueError(f"unsupported MPQ header version {format_version}")
        if not hash_count or hash_count & (hash_count - 1):
            raise ValueError("MPQ hash table size is not a power of two")
        self.data = data
        self.sector_size = 512 << sector_shift
        self.hash_count = hash_count
        self.block_count = block_count
        self.hash_table = _decrypt_mpq_block(
            data[hash_pos : hash_pos + hash_count * 16], 0xC3AF3770
        )
        self.block_table = _decrypt_mpq_block(
            data[block_pos : block_pos + block_count * 16], 0xEC83B3A3
        )
        if len(self.hash_table) != hash_count * 16 or len(self.block_table) != block_count * 16:
            raise ValueError("truncated MPQ tables")
        if archive_size > len(data):
            raise ValueError("truncated MPQ archive")

    @staticmethod
    def _crypt_table() -> list[int]:
        return _mpq_crypt_table()

    def _hash(self, name: str, kind: int) -> int:
        crypt = self._crypt_table()
        seed1 = 0x7FED7FED
        seed2 = 0xEEEEEEEE
        for char in name.upper().encode("latin-1"):
            seed1 = (crypt[kind * 0x100 + char] ^ (seed1 + seed2)) & 0xFFFFFFFF
            seed2 = (char + seed1 + seed2 + (seed2 << 5) + 3) & 0xFFFFFFFF
        return seed1

    def _find_block(self, name: str) -> int:
        start = self._hash(name, 0) & (self.hash_count - 1)
        wanted_a = self._hash(name, 1)
        wanted_b = self._hash(name, 2)
        for offset in range(self.hash_count):
            index = (start + offset) & (self.hash_count - 1)
            hash_a, hash_b, _locale, block_index = struct.unpack_from(
                "<IIII", self.hash_table, index * 16
            )
            if block_index == 0xFFFFFFFF:
                return -1
            if hash_a == wanted_a and hash_b == wanted_b:
                return block_index
        return -1

    def _decompress_sector(self, data: bytes, expected_size: int, name: str) -> bytes:
        if len(data) >= expected_size:
            return data[:expected_size]
        if not data:
            raise ValueError(f"empty compressed MPQ sector: {name}")
        methods = data[0]
        payload = data[1:]
        if methods & 0x02:
            return zlib.decompress(payload)[:expected_size]
        if methods & 0x10:
            return bz2.decompress(payload)[:expected_size]
        raise ValueError(f"unsupported MPQ compression 0x{methods:02x}: {name}")

    def read(self, name: str) -> bytes:
        block_index = self._find_block(name)
        if block_index < 0 or block_index >= self.block_count:
            raise KeyError(name)
        file_pos, compressed_size, file_size, flags = struct.unpack_from(
            "<IIII", self.block_table, block_index * 16
        )
        if not flags & 0x80000000:
            raise ValueError(f"MPQ member is not present: {name}")
        key = self._hash(name, 3)
        if flags & 0x00020000:
            key = ((key + file_pos) ^ file_size) & 0xFFFFFFFF

        if flags & 0x01000000:  # MPQ_FILE_SINGLE_UNIT
            chunk = self.data[file_pos : file_pos + compressed_size]
            if flags & 0x00010000:
                chunk = _decrypt_mpq_block(chunk, key)
            if flags & 0x00000200:
                return self._decompress_sector(chunk, file_size, name)
            return chunk[:file_size]

        sector_count = (file_size + self.sector_size - 1) // self.sector_size
        table_size = (sector_count + 1) * 4
        offsets = self.data[file_pos : file_pos + table_size]
        if flags & 0x00010000:
            offsets = _decrypt_mpq_block(offsets, (key + 1) & 0xFFFFFFFF)
        if len(offsets) != table_size:
            raise ValueError(f"truncated MPQ sector table: {name}")
        positions = struct.unpack("<" + "I" * (sector_count + 1), offsets)
        result = bytearray()
        for index in range(sector_count):
            chunk = self.data[file_pos + positions[index] : file_pos + positions[index + 1]]
            expected = min(self.sector_size, file_size - index * self.sector_size)
            if flags & 0x00010000:
                chunk = _decrypt_mpq_block(chunk, (key + index + 1) & 0xFFFFFFFF)
            if flags & 0x00000200:
                chunk = self._decompress_sector(chunk, expected, name)
            result.extend(chunk[:expected])
        return bytes(result[:file_size])

    def names(self) -> list[str]:
        try:
            listing = self.read("(listfile)").decode("latin-1")
        except (KeyError, UnicodeDecodeError) as error:
            raise ValueError("MPQ has no readable (listfile) entry") from error
        names = [line.strip().replace("\\", "/") for line in listing.splitlines()]
        return [name for name in names if name and not name.startswith("(")]

    def members(self) -> dict[str, bytes]:
        return {name: self.read(name) for name in self.names()}


def archive_members(data: bytes) -> dict[str, bytes]:
    if data.startswith(MPQ_MAGIC):
        return MpqArchive(data).members()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return {
                info.filename: archive.read(info.filename)
                for info in archive.infolist()
                if not info.is_dir()
            }
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError("pack member is neither a ZIP O2R nor a supported MPQ OTR") from error


@dataclass
class PackSource:
    origin: str
    members: dict[str, bytes]


def collect_sources(path: Path) -> list[PackSource]:
    """Open a direct pack or an author's download ZIP in its implied order."""
    data = path.read_bytes()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            pack_infos = [
                info
                for info in archive.infolist()
                if not info.is_dir() and info.filename.casefold().endswith(O2R_SUFFIXES)
            ]
    except zipfile.BadZipFile:
        pack_infos = []

    if not pack_infos:
        return [PackSource(str(path), archive_members(data))]

    pack_infos.sort(key=lambda info: natural_key(info.filename))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return [
            PackSource(f"{path}:{info.filename}", archive_members(archive.read(info.filename)))
            for info in pack_infos
        ]


def resource_headers(oot: Path) -> tuple[bytes, bytes]:
    """Take the exact resource header layout from the user's own O2R."""
    with zipfile.ZipFile(oot) as archive:
        display_header = None
        vertex_header = None
        for info in archive.infolist():
            if info.is_dir():
                continue
            header = archive.read(info.filename)[:64]
            if len(header) != 64:
                continue
            if header[4:8] == b"TLDO" and display_header is None:
                display_header = header
            elif header[4:8] == b"XTVO" and vertex_header is None:
                vertex_header = header
            if display_header is not None and vertex_header is not None:
                break
    if display_header is None:
        raise ValueError(f"{oot}: no binary DisplayList resource header found")
    if vertex_header is None:
        vertex_header = display_header[:4] + b"XTVO" + display_header[8:]
    return display_header, vertex_header


def build_lookup_index(oot: Path, packs: Iterable[PackSource]) -> tuple[set[str], set[str]]:
    with zipfile.ZipFile(oot) as archive:
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
    base: set[str] = set()
    alt: set[str] = set()
    for name in [*names, *(name for pack in packs for name in pack.members)]:
        if name.startswith("alt/"):
            alt.add(name[4:])
        else:
            base.add(name)
    return base, alt


def transform_members(
    members: dict[str, bytes],
    index: tuple[set[str], set[str]],
    headers: tuple[bytes, bytes],
    max_size: int | None = None,
) -> tuple[dict[str, bytes], dict[str, object]]:
    """Apply XML conversion and optional downscaling to one O2R member map."""
    transformed: dict[str, bytes] = {}
    converted = 0
    conversion_failures: list[str] = []
    downscaled = 0
    for name, original in members.items():
        data = original
        if data.lstrip().startswith(b"<"):
            try:
                root = ET.fromstring(data)
            except ET.ParseError:
                root = None
            if root is not None and root.tag in ("DisplayList", "Vertex"):
                try:
                    data = (
                        xml_to_binary.display_list(data, index, name, headers[0])
                        if root.tag == "DisplayList"
                        else xml_to_binary.vertex(data, name, headers[1])
                    )
                    converted += 1
                except Exception as error:  # a pack-specific XML failure must stay XML
                    conversion_failures.append(f"{name}: {type(error).__name__}: {error}")
        if max_size is not None:
            rewritten = downscale.downscale_resource(data, max_size)
            downscaled += rewritten != data
            data = rewritten
        transformed[name] = data
    return transformed, {
        "xml_converted": converted,
        "xml_conversion_failures": conversion_failures,
        "downscaled": downscaled,
    }


def downscale_members(members: dict[str, bytes], max_size: int | None) -> tuple[dict[str, bytes], int]:
    """Downscale final archive members, leaving BC/unsupported resources unchanged."""
    if max_size is None:
        return members, 0
    rewritten: dict[str, bytes] = {}
    changed = 0
    for name, data in members.items():
        try:
            new_data = downscale.downscale_resource(data, max_size)
        except (ValueError, struct.error):
            new_data = data
        changed += new_data != data
        rewritten[name] = new_data
    return rewritten, changed


def _bc_job_results(tasks: list[tuple[str, bytes, bool, bool]], jobs: int):
    if jobs == 1:
        yield from (bc._work(task) for task in tasks)
        return
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        yield from pool.map(bc._work, tasks, chunksize=16)


def build_bc_members(members: dict[str, bytes], jobs: int) -> tuple[dict[str, bytes], dict[str, int]]:
    """Return the eligible BC members from one pack, keeping their paths."""
    tasks = [(name, data, False, True) for name, data in sorted(members.items())]
    output: dict[str, bytes] = {}
    counts: dict[str, int] = {}
    for name, data, reason in _bc_job_results(tasks, jobs):
        counts[reason] = counts.get(reason, 0) + 1
        if data is not None:
            output[name] = data
    return output, counts


def build_bc_override(packs: list[PackSource], jobs: int) -> tuple[dict[str, bytes], dict[str, int]]:
    """Build the legacy global override for callers that still need it.

    The pack tool no longer emits this overlay: its normal output uses
    :func:`build_bc_members` separately for every source pack.
    """
    winners: dict[str, bytes] = {}
    for pack in packs:
        winners.update(pack.members)
    return build_bc_members(winners, jobs)


def build_blank_skyboxes(oot: Path, djipi_main: dict[str, bytes]) -> dict[str, bytes]:
    template_name = "alt/textures/vr_KSVR_static/gKokiriShopBgTex"
    try:
        header = djipi_main[template_name][:64]
    except KeyError as error:
        raise ValueError(f"Djipi main pack lacks {template_name}; cannot build skybox fix") from error
    with zipfile.ZipFile(oot) as oot_archive:
        output: dict[str, bytes] = {}
        for directory, image_name, palette_name in PAIRS:
            width, height, indices = texture(oot_archive, f"textures/{directory}_static/{image_name}")
            colors = palette(oot_archive, f"textures/{directory}_pal_static/{palette_name}")
            image = Image.new("RGBA", (width, height))
            image.putdata([colors[index] for index in indices])
            pixels = image.resize((512, 512), Image.Resampling.LANCZOS).tobytes()
            output[f"alt/textures/{directory}_static/{image_name}"] = header + struct.pack(
                "<IIIIffI", 1, 512, 512, 1, 8.0, 2.0, len(pixels)
            ) + pixels
    return output


def write_archive(path: Path, members: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def write_load_order(out: Path, filenames: list[str]) -> str:
    enabled = "|".join(Path(filename).stem for filename in filenames)
    lines = [
        "# SoH Wii U mod load order: first to last; later archives win.",
        *filenames,
        "",
        f"EnabledMods={enabled}",
    ]
    (out / "LOAD_ORDER.txt").write_text("\n".join(lines) + "\n")
    return enabled


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--oot", type=Path, required=True, help="your SoH 9.2.3 oot.o2r")
    parser.add_argument("--out", type=Path, required=True, help="output Wii U mods directory")
    parser.add_argument("packs", nargs="+", type=Path, metavar="PACK_OR_ZIP")
    parser.add_argument("--jobs", type=int, default=1, help="BC encoder processes (capped from available memory)")
    parser.add_argument("--max-size", type=int, help="maximum texture dimension; repeatedly halves oversized raw RGBA textures")
    args = parser.parse_args(argv)
    if args.max_size is not None and args.max_size < 1:
        parser.error("--max-size must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.monotonic()
    if not args.oot.is_file():
        raise SystemExit(f"error: missing --oot archive: {args.oot}")
    if args.out.resolve() == args.oot.resolve():
        raise SystemExit("error: --out must differ from --oot")
    jobs = bounded_jobs(args.jobs)
    if jobs != args.jobs:
        print(f"capped --jobs {args.jobs} to {jobs} from available memory", file=sys.stderr)

    sources = [source for path in args.packs for source in collect_sources(path)]
    if not sources:
        raise SystemExit("error: no .o2r/.otr pack members found")
    headers = resource_headers(args.oot)
    index = build_lookup_index(args.oot, sources)

    args.out.mkdir(parents=True, exist_ok=True)
    output_names: list[str] = []
    for source in sources:
        # The specified order is XML conversion -> BC stack -> optional
        # downscale.  Keep the pre-downscale bytes for BC conversion.
        transformed, stats = transform_members(source.members, index, headers)
        output_members, downscaled = downscale_members(transformed, args.max_size)
        bc_members, bc_counts = build_bc_members(transformed, jobs)
        bc_members, bc_downscaled = downscale_members(bc_members, args.max_size)
        output_members.update(bc_members)
        filename = f"{source_pack_name(source.origin)}.o2r"
        if filename in output_names:
            raise SystemExit(f"error: source packs collide after FAT32 filename conversion: {filename}")
        write_archive(args.out / filename, output_members)
        output_names.append(filename)
        failures = stats["xml_conversion_failures"]
        suffix = f", XML failures={len(failures)}" if failures else ""
        print(
            f"{filename}: XML converted={stats['xml_converted']}, "
            f"BC={dict(sorted(bc_counts.items()))}, emitted={len(bc_members)}, "
            f"downscaled={downscaled + bc_downscaled}{suffix}"
        )

    if any("djipi" in source.origin.casefold() for source in sources):
        main_pack = next(
            (source.members for source in sources if "01 main textures" in source.origin.casefold()),
            None,
        )
        if main_pack is None:
            raise SystemExit("error: Djipi input detected but its 01 Main Textures pack is missing")
        skybox = build_blank_skyboxes(args.oot, main_pack)
        skybox, skybox_downscaled = downscale_members(skybox, args.max_size)
        fix_filename = "zz-fix-blank-skyboxes.o2r"
        write_archive(args.out / fix_filename, skybox)
        output_names.append(fix_filename)
        print(f"{fix_filename}: emitted={len(skybox)}, downscaled={skybox_downscaled}")

    output_names.sort(key=profile_sort_key)
    enabled = write_load_order(args.out, output_names)
    print(f"\nCopy these files to sd:/wiiu/apps/soh923/mods/:")
    for filename in output_names:
        print(f"  {filename}")
    print(f"EnabledMods={enabled}")
    print(f"completed in {time.monotonic() - started:.1f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
