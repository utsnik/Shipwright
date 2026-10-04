#!/usr/bin/env python3
"""Convert raw RGBA8888 HD textures in an O2R pack to BC1/BC3 (see docs/2026-10-02-bc-texture-format.md).

Writes ONLY the converted textures to the output archive, as an override pack to load after the source pack.
Fully opaque textures become BC1; any alpha (including 1-bit) becomes BC3, because BC1 punch-through decodes
alpha-0 texels as black and opaque N64 materials (RGBA16 with an ignored 1-bit alpha) sample that colour.
Pillow (>= 11) does the DXT encoding.
"""
from __future__ import annotations

import argparse
import io
import struct
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from PIL import Image

OTR_HEADER_SIZE = 64
TEX_FLAG_LOAD_AS_RAW = 1 << 0
TEX_FLAG_LOAD_AS_IMG = 1 << 1
TEX_FLAG_BC1 = 1 << 4
TEX_FLAG_BC3 = 1 << 5
DDS_HEADER_SIZE = 128  # "DDS " + 124-byte header; Pillow writes no DX10 extension for DXT1/DXT5
# LUS TextureType -> N64 bits per pixel (RGBA32, RGBA16, CI4, CI8, I4, I8, IA4, IA8, IA16)
TEXTURE_BPP = {1: 32, 2: 16, 3: 4, 4: 8, 5: 4, 6: 8, 7: 4, 8: 8, 9: 16}
# Textures the game registers with Gfx_RegisterBlendedTexture (soh/src: Boss_Dodongo, Boss_Goma, Boss_Ganon,
# Boss_Ganondrof, En_Ganon_Mant). A blended texture with a replacement goes through the interpreter's
# importReplacement path, which cannot take BC data (the texture would not be drawn), so never convert them.
BLENDED_NAMES = ("gDodongosCavernBossLavaFloorTex", "gMantTex", "ganon_boss_sceneTex_006C18",
                 "ganon_boss_sceneTex_007418", "gGohmaBodyTex", "gGohmaShellUndersideTex", "gGohmaDarkShellTex",
                 "gGohmaEyeTex", "gGohmaShellTex", "gGohmaIrisTex", "gPhantomGanonLimbTex_00B380",
                 "gPhantomGanonEyeTex", "sLimbTex_", "sMouthTex_ci8_")


def skip_reason(data: bytes, whole_only: bool = False) -> str | None:
    """Why a resource must stay as it is, or None when convert() may turn it into BC."""
    if len(data) < OTR_HEADER_SIZE + 28 or data[4:8] != b"XETO":
        return "not-texture"
    order = "<" if data[0] == 0 else ">"
    if struct.unpack_from(order + "I", data, 8)[0] != 1:
        return "not-hd"
    tex_type, width, height, flags = struct.unpack_from(order + "IIII", data, OTR_HEADER_SIZE)
    h_scale, v_scale = struct.unpack_from(order + "ff", data, OTR_HEADER_SIZE + 16)
    # The interpreter takes the BC path only for LOAD_AS_RAW without LOAD_AS_IMG (Interpreter::ImportTexture).
    if not flags & TEX_FLAG_LOAD_AS_RAW or flags & TEX_FLAG_LOAD_AS_IMG or flags & (TEX_FLAG_BC1 | TEX_FLAG_BC3):
        return "flags"
    if len(data) - (OTR_HEADER_SIZE + 28) < width * height * 4:
        return "not-rgba"
    if width % 4 or height % 4:
        return "dims"
    if whole_only and h_scale > 0 and v_scale > 0:
        # N64 textures larger than TMEM (4 KiB, 2 KiB for CI) are loaded in parts; those take the interpreter's
        # crop path, which no hardware test has exercised yet.
        orig = (width / h_scale) * (height / v_scale) * TEXTURE_BPP.get(tex_type, 16) / 8
        if orig > (2048 if tex_type in (3, 4) else 4096):
            return "partial-load"
    return None


def convert(data: bytes, opaque_only: bool = False) -> tuple[bytes, str] | None:
    """Return (new resource bytes, format) for a raw RGBA texture, or None to leave it out."""
    if skip_reason(data) is not None:
        return None
    order = "<" if data[0] == 0 else ">"
    tex_type, width, height, flags = struct.unpack_from(order + "IIII", data, OTR_HEADER_SIZE)
    h_scale, v_scale = struct.unpack_from(order + "ff", data, OTR_HEADER_SIZE + 16)
    pixel_offset = OTR_HEADER_SIZE + 28
    rgba = np.frombuffer(data, dtype=np.uint8, count=width * height * 4, offset=pixel_offset).reshape(height, width, 4)
    alpha = rgba[..., 3]
    # Any alpha -> BC3. Kokiri 2026-10-02: kusa_04 (64% alpha 0, real grass colour underneath) drew black patches
    # on the ground as BC1, because the N64 material ignores the RGBA16 alpha bit and samples the RGB.
    # Alpha >= 250 everywhere counts as opaque (yuka floor is 253..255).
    soft = alpha.min() < 250
    if soft and opaque_only:
        return None  # BC3 still draws wrong colours on the console (2026-10-02); keep alpha textures raw RGBA
    fmt, flag, block_bytes = ("DXT5", TEX_FLAG_BC3, 16) if soft else ("DXT1", TEX_FLAG_BC1, 8)
    buffer = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buffer, "DDS", pixel_format=fmt)
    blocks = buffer.getvalue()[DDS_HEADER_SIZE:]
    expected = (width // 4) * (height // 4) * block_bytes
    if len(blocks) != expected:
        raise ValueError(f"unexpected DDS payload {len(blocks)} != {expected}")
    body = struct.pack(order + "IIIIffI", tex_type, width, height, flags | flag, h_scale, v_scale, len(blocks))
    return data[:OTR_HEADER_SIZE] + body + blocks, ("BC3" if soft else "BC1")


def _work(job: tuple[str, bytes, bool, bool]) -> tuple[str, bytes | None, str]:
    name, data, opaque_only, whole_only = job
    base = name.rsplit("/", 1)[-1]
    if any(base == n or (n.endswith("_") and base.startswith(n)) for n in BLENDED_NAMES):
        return name, None, "blended"
    reason = skip_reason(data, whole_only)
    if reason is not None:
        return name, None, reason
    result = convert(data, opaque_only)
    if result is None:
        return name, None, "alpha" if opaque_only else "unconverted"
    return name, result[0], result[1]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--prefix", action="append", default=[], help="only convert entries under this path (repeatable)")
    p.add_argument("--opaque-only", action="store_true", help="convert only opaque textures (BC1); leave alpha ones out")
    p.add_argument("--whole-only", action="store_true",
                   help="leave out textures larger than TMEM (loaded in parts on the N64)")
    p.add_argument("--jobs", type=int, default=1, help="encoder processes")
    a = p.parse_args(argv)
    start = time.time()
    counts: dict[str, int] = {}
    before = after = 0
    with zipfile.ZipFile(a.input) as src, zipfile.ZipFile(a.output, "w", zipfile.ZIP_DEFLATED) as dst:
        names = [i.filename for i in src.infolist()
                 if not i.is_dir() and (not a.prefix or any(i.filename.startswith(x) for x in a.prefix))]
        jobs = ((n, src.read(n), a.opaque_only, a.whole_only) for n in names)
        with ProcessPoolExecutor(max_workers=max(1, a.jobs)) as pool:
            for name, new, tag in pool.map(_work, jobs, chunksize=16):
                counts[tag] = counts.get(tag, 0) + 1
                if new is None:
                    continue
                dst.writestr(name, new)
                before += src.getinfo(name).file_size
                after += len(new)
    print(f"{a.output}: {dict(sorted(counts.items()))}; converted raw {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB; "
          f"{time.time() - start:.0f} s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
