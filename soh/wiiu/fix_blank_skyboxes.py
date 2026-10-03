#!/usr/bin/env python3
"""Build zz-fix-blank-skyboxes.o2r: replaces the texture-pack skybox images that are fully transparent.

Djipi's 3DS Experience ("01 Main Textures") ships gKokiriShopBgTex/gKokiriShop2BgTex and the three
gCarpentersTent*BgTex as 100% transparent white (FFFFFF00). The shop-browsing view (talking to the Kokiri
shopkeeper) and the Carpenters' Tent draw that skybox opaque, so the screen turns white.

This rebuilds those five images from the player's own oot.o2r (CI8 + RGBA16 palette -> RGBA32, 2x upscale to
512) with the same LUS header layout as the pack's 512-cap entries. Mods load alphabetically and the last
archive wins, so dropping the result into mods/ overrides the blank ones. Nothing from the ROM is shipped:
run it against your own oot.o2r.

usage: soh_fix_blank_skyboxes.py OOT_O2R DJIPI_MAIN_TEXTURES_O2R OUT_O2R
  DJIPI_MAIN_TEXTURES_O2R = the pack's "01 Main Textures" .o2r (only the 64-byte resource header of its
  Kokiri shop entry is reused), or that entry extracted to a file
"""
import struct
import sys
import zipfile

from PIL import Image

PAIRS = [
    ("vr_KSVR", "gKokiriShopBgTex", "gKokiriShopBgTLUT"),
    ("vr_KSVR", "gKokiriShop2BgTex", "gKokiriShopBg2TLUT"),
    ("vr_TTVR", "gCarpentersTentBgTex", "gCarpentersTentBgTLUT"),
    ("vr_TTVR", "gCarpentersTent2BgTex", "gCarpentersTentBg2TLUT"),
    ("vr_TTVR", "gCarpentersTent3BgTex", "gCarpentersTentBg3TLUT"),
]


def texture(oot, name):
    d = oot.read(name)
    _, w, h, size = struct.unpack("<IIII", d[64:80])
    return w, h, d[80:80 + size]


def palette(oot, name):
    _, _, d = texture(oot, name)
    out = []
    for i in range(0, len(d), 2):
        v = (d[i] << 8) | d[i + 1]  # RGBA5551, big-endian as on the N64
        out.append((((v >> 11) & 31) * 255 // 31, ((v >> 6) & 31) * 255 // 31, ((v >> 1) & 31) * 255 // 31,
                    255 if v & 1 else 0))
    return out


def main():
    oot_path, template_path, out_path = sys.argv[1:4]
    oot = zipfile.ZipFile(oot_path)
    if zipfile.is_zipfile(template_path):
        header = zipfile.ZipFile(template_path).read("alt/textures/vr_KSVR_static/gKokiriShopBgTex")[:64]
    else:
        try:
            from mpyq import MPQArchive
        except ImportError:
            print("pip install mpyq", file=sys.stderr)
            raise SystemExit(1)
        pack = MPQArchive(template_path)
        candidates = (
            "alt/textures/vr_KSVR_static/gKokiriShopBgTex",
            "textures/vr_KSVR_static/gKokiriShopBgTex",
        )
        for name in candidates:
            try:
                header = pack.read_file(name)[:64]
                break
            except KeyError:
                continue
        else:
            raise KeyError("gKokiriShopBgTex not found in MPQ")
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as out:
        for d, tex, tlut in PAIRS:
            w, h, idx = texture(oot, f"textures/{d}_static/{tex}")
            pal = palette(oot, f"textures/{d}_pal_static/{tlut}")
            img = Image.new("RGBA", (w, h))
            img.putdata([pal[i] for i in idx])
            px = img.resize((512, 512), Image.LANCZOS).tobytes()
            # type RGBA32, 512x512, flags=1 (load as raw), hByteScale 8, vPixelScale 2 (512 over a 256 CI8 image)
            out.writestr(f"alt/textures/{d}_static/{tex}", header + struct.pack("<IIIIffI", 1, 512, 512, 1, 8.0, 2.0,
                                                                                   len(px)) + px)
            print(tex, "ok")


if __name__ == "__main__":
    main()
