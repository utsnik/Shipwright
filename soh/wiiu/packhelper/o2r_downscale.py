#!/usr/bin/env python3
"""Downscale oversized RGBA textures in a ZIP-based O2R archive."""

from __future__ import annotations

import argparse
import struct
import sys
import zipfile
from pathlib import Path


OTR_HEADER_SIZE = 64
TEXTURE_MAGIC = b"XETO"
RGBA32 = 1
RGBA16 = 2


def _byte_order(header: bytes) -> str:
    if header[0] == 0:
        return "<"
    if header[0] == 1:
        return ">"
    raise ValueError(f"invalid OTR endianness byte {header[0]}")


def _downscale_rgba32(pixels: bytes, width: int, height: int) -> tuple[bytes, int, int]:
    new_width = max(1, width // 2)
    new_height = max(1, height // 2)
    result = bytearray(new_width * new_height * 4)

    destination = 0
    for y in range(new_height):
        y0 = y * 2
        y1 = min(y0 + 1, height - 1)
        row0 = y0 * width * 4
        row1 = y1 * width * 4
        for x in range(new_width):
            x0 = x * 2 * 4
            x1 = min(x * 2 + 1, width - 1) * 4
            offsets = (row0 + x0, row0 + x1, row1 + x0, row1 + x1)
            for channel in range(4):
                result[destination + channel] = sum(
                    pixels[offset + channel] for offset in offsets
                ) // 4
            destination += 4

    return bytes(result), new_width, new_height


def _unpack_rgba5551(
    pixels: bytes, offset: int, order: str
) -> tuple[int, int, int, int]:
    value = struct.unpack_from(f"{order}H", pixels, offset)[0]
    return value >> 11, (value >> 6) & 0x1F, (value >> 1) & 0x1F, value & 1


def _downscale_rgba16(
    pixels: bytes, width: int, height: int, order: str
) -> tuple[bytes, int, int]:
    new_width = max(1, width // 2)
    new_height = max(1, height // 2)
    result = bytearray(new_width * new_height * 2)

    destination = 0
    for y in range(new_height):
        y0 = y * 2
        y1 = min(y0 + 1, height - 1)
        row0 = y0 * width * 2
        row1 = y1 * width * 2
        for x in range(new_width):
            x0 = x * 2 * 2
            x1 = min(x * 2 + 1, width - 1) * 2
            samples = (
                _unpack_rgba5551(pixels, row0 + x0, order),
                _unpack_rgba5551(pixels, row0 + x1, order),
                _unpack_rgba5551(pixels, row1 + x0, order),
                _unpack_rgba5551(pixels, row1 + x1, order),
            )
            red, green, blue = (
                sum(sample[channel] for sample in samples) // 4
                for channel in range(3)
            )
            # Quantize the averaged one-bit alpha channel to nearest. This
            # retains opaque coverage when at least half of the texels are
            # opaque instead of making anything short of 4/4 transparent.
            alpha = (sum(sample[3] for sample in samples) + 2) // 4
            value = (red << 11) | (green << 6) | (blue << 1) | alpha
            struct.pack_into(f"{order}H", result, destination, value)
            destination += 2

    return bytes(result), new_width, new_height


def downscale_resource(data: bytes, max_dim: int) -> bytes:
    """Return one archive member, downscaling it when it is an oversized texture."""
    if len(data) < OTR_HEADER_SIZE or data[4:8] != TEXTURE_MAGIC:
        return data

    order = _byte_order(data[:OTR_HEADER_SIZE])
    version = struct.unpack_from(f"{order}I", data, 8)[0]
    if version == 0:
        body_size = 16
        image_size_offset = OTR_HEADER_SIZE + 12
    elif version == 1:
        body_size = 28
        image_size_offset = OTR_HEADER_SIZE + 24
    else:
        return data

    pixel_offset = OTR_HEADER_SIZE + body_size
    if len(data) < pixel_offset:
        raise ValueError(f"truncated version {version} texture body")

    texture_type, width, height = struct.unpack_from(
        f"{order}III", data, OTR_HEADER_SIZE
    )
    if max(width, height) <= max_dim:
        return data
    if version == 0:
        print(
            "o2r_downscale.py: version 0 texture requires downscaling; "
            "leaving it unchanged",
            file=sys.stderr,
        )
        return data

    image_size = struct.unpack_from(f"{order}I", data, image_size_offset)[0]
    pixel_count = width * height
    if pixel_count == 0:
        raise ValueError(
            f"texture has no pixels for {width}x{height} type {texture_type}"
        )
    bytes_per_pixel, remainder = divmod(image_size, pixel_count)
    if remainder or bytes_per_pixel not in (2, 4):
        raise ValueError(
            f"texture has {image_size} image bytes for {width}x{height}; "
            f"unsupported inferred bytes-per-pixel {image_size}/{pixel_count}"
        )
    if len(data) < pixel_offset + image_size:
        raise ValueError(
            f"texture contains {len(data) - pixel_offset} image bytes, "
            f"expected {image_size}"
        )

    pixels = data[pixel_offset : pixel_offset + image_size]
    h_byte_scale = struct.unpack_from(
        f"{order}f", data, OTR_HEADER_SIZE + 16
    )[0]
    v_pixel_scale = struct.unpack_from(
        f"{order}f", data, OTR_HEADER_SIZE + 20
    )[0]
    while max(width, height) > max_dim:
        if bytes_per_pixel == 4:
            pixels, width, height = _downscale_rgba32(pixels, width, height)
        else:
            pixels, width, height = _downscale_rgba16(
                pixels, width, height, order
            )
        h_byte_scale /= 2
        v_pixel_scale /= 2

    result = bytearray(data[:pixel_offset])
    struct.pack_into(f"{order}I", result, OTR_HEADER_SIZE + 4, width)
    struct.pack_into(f"{order}I", result, OTR_HEADER_SIZE + 8, height)
    struct.pack_into(f"{order}f", result, OTR_HEADER_SIZE + 16, h_byte_scale)
    struct.pack_into(f"{order}f", result, OTR_HEADER_SIZE + 20, v_pixel_scale)
    struct.pack_into(f"{order}I", result, image_size_offset, len(pixels))
    result.extend(pixels)
    result.extend(data[pixel_offset + image_size :])
    return bytes(result)


def rewrite_archive(input_path: Path, output_path: Path, max_dim: int) -> None:
    if input_path.resolve() == output_path.resolve():
        raise ValueError("input and output paths must be different")

    with zipfile.ZipFile(input_path, "r") as source, zipfile.ZipFile(
        output_path, "w", compression=zipfile.ZIP_DEFLATED
    ) as destination:
        for info in source.infolist():
            data = source.read(info)
            try:
                rewritten = downscale_resource(data, max_dim)
            except (ValueError, struct.error) as error:
                print(
                    f"o2r_downscale.py: {info.filename}: {error}",
                    file=sys.stderr,
                )
                rewritten = data
            destination.writestr(
                info,
                rewritten,
                compress_type=zipfile.ZIP_DEFLATED,
            )


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Downscale oversized RGBA textures in an O2R archive."
    )
    parser.add_argument("input", type=Path, help="input .o2r ZIP archive")
    parser.add_argument("output", type=Path, help="output .o2r path")
    parser.add_argument(
        "--max-dim",
        type=positive_int,
        default=1024,
        help="maximum texture width or height (default: 1024)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        rewrite_archive(args.input, args.output, args.max_dim)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"o2r_downscale.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
