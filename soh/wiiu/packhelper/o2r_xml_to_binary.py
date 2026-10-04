#!/usr/bin/env python3
"""Convert legacy XML DisplayList and Vertex O2R members to binary OTR resources."""

from __future__ import annotations

import argparse
import os
import re
import struct
import tempfile
from functools import cache
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

def crc64(path: str) -> int:
    """Ship CRC64 (CRC-64/ECMA, all-one initial state and final complement)."""
    crc = 0xFFFFFFFFFFFFFFFF
    for byte in path.encode("utf-8"):
        crc ^= byte << 56
        for _ in range(8):
            crc = ((crc << 1) ^ (0x42F0E1EBA9EA3693 if crc & (1 << 63) else 0)) & 0xFFFFFFFFFFFFFFFF
    return crc


def members(path: Path) -> set[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            return set(archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return set()


def loaded_members(input_path: Path) -> tuple[set[str], set[str]]:
    base: set[str] = set()
    alt: set[str] = set()
    paths = [input_path]
    seen = set()
    for path in paths:
        resolved = str(path.resolve())
        if resolved in seen or not path.exists():
            continue
        seen.add(resolved)
        for name in members(path):
            if name.startswith("alt/"):
                alt.add(name[4:])
            else:
                base.add(name)
    return base, alt


UNRESOLVED_TEXTURES: set[str] = set()
UNRESOLVED_REFERENCES: set[str] = set()


def resolve_path(path: str, indexes: tuple[set[str], set[str]]) -> tuple[str, int]:
    base, alt = indexes
    # P is checked globally before any alt/P candidate, matching ResourceManager's lookup.
    if path in base:
        resolved = path
    elif path in alt:
        resolved = "alt/" + path
    else:
        raise ValueError(f"unresolved resource path: {path}")
    return resolved, crc64(resolved)


def reference_hash(path: str, indexes: tuple[set[str], set[str]]) -> int:
    """Hash the exact XML lookup key when a referenced resource is absent.

    The XML factory preserves the requested path and lets ResourceManager report
    a missing resource later. A binary hash of that same path preserves that
    failure behavior. Texture images have their separate XML PipeSync fallback.
    """
    try:
        return resolve_path(path, indexes)[1]
    except ValueError:
        UNRESOLVED_REFERENCES.add(path)
        return crc64(path)


def pack_words(words: list[tuple[int, int]]) -> bytes:
    return b"".join(struct.pack("<II", a & 0xFFFFFFFF, b & 0xFFFFFFFF) for a, b in words)


def _i(e: ET.Element, key: str, default: int = 0) -> int:
    raw = e.get(key)
    if raw is None:
        return default
    # tinyxml2 parses the decimal prefix through a signed 64-bit value, then
    # narrows to int32. Preserve the platform's two's-complement wrap for
    # values outside int32 and its signed-64 saturation for larger inputs.
    match = re.match(r"\s*([+-]?\d+)", raw)
    if match is None:
        return 0
    value = max(-0x8000000000000000, min(0x7FFFFFFFFFFFFFFF, int(match.group(1))))
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _has(e: ET.Element, key: str) -> bool:
    # tinyxml2's Attribute(name, nullptr) checks presence, not truthiness.
    return key in e.attrib


def _enum(e: ET.Element, key: str, values: dict[str, int], default: int) -> int:
    return values.get(e.get(key, ""), default)


def _cmd(op: int, low: int = 0, w1: int = 0) -> tuple[int, int]:
    return ((op << 24) | (low & 0xFFFFFF), w1 & 0xFFFFFFFF)


def display_list(
    data: bytes,
    index: tuple[set[str], set[str]],
    member: str,
    header: bytes | None = None,
) -> bytes:
    root = ET.fromstring(data)
    if root.tag != "DisplayList":
        raise ValueError(f"{member}: expected DisplayList XML")
    out: list[tuple[int, int]] = []
    for e in root:
        tag = e.tag
        a, b = e.attrib, None
        if tag == "PipeSync": out.append(_cmd(0xE7))
        elif tag == "TileSync": out.append(_cmd(0xE8))
        elif tag == "LoadSync": out.append(_cmd(0xE6))
        elif tag == "EndDisplayList": out.append(_cmd(0xDF))
        elif tag == "Texture":
            out.append(_cmd(0xD7, (_i(e,"Level") << 11) | (_i(e,"Tile") << 8) | (_i(e,"On") << 1), (_i(e,"S") << 16) | _i(e,"T")))
        elif tag == "SetPrimColor":
            rgb = (_i(e,"R") << 24) | (_i(e,"G") << 16) | (_i(e,"B") << 8) | _i(e,"A")
            out.append(_cmd(0xFA, (_i(e,"M") << 8) | _i(e,"L"), rgb))
        elif tag == "SetEnvColor":
            out.append(_cmd(0xFB, 0, (_i(e,"R") << 24) | (_i(e,"G") << 16) | (_i(e,"B") << 8) | _i(e,"A")))
        elif tag == "SetCombineLERP":
            # Fast::ResourceFactoryDisplayList::GetCombineLERPValue, then gsDPSetCombineLERP_NoMacros.
            mux = {"G_CCMUX_COMBINED":0,"G_CCMUX_TEXEL0":1,"G_CCMUX_TEXEL1":2,"G_CCMUX_PRIMITIVE":3,"G_CCMUX_SHADE":4,"G_CCMUX_ENVIRONMENT":5,"G_CCMUX_1":6,"G_CCMUX_NOISE":7,"G_CCMUX_0":31,"G_CCMUX_CENTER":6,"G_CCMUX_K4":7,"G_CCMUX_SCALE":6,"G_CCMUX_COMBINED_ALPHA":7,"G_CCMUX_TEXEL0_ALPHA":8,"G_CCMUX_TEXEL1_ALPHA":9,"G_CCMUX_PRIMITIVE_ALPHA":10,"G_CCMUX_SHADE_ALPHA":11,"G_CCMUX_ENV_ALPHA":12,"G_CCMUX_LOD_FRACTION":13,"G_CCMUX_PRIM_LOD_FRAC":14,"G_CCMUX_K5":15,"G_ACMUX_COMBINED":0,"G_ACMUX_TEXEL0":1,"G_ACMUX_TEXEL1":2,"G_ACMUX_PRIMITIVE":3,"G_ACMUX_SHADE":4,"G_ACMUX_ENVIRONMENT":5,"G_ACMUX_1":6,"G_ACMUX_0":7,"G_ACMUX_LOD_FRACTION":0,"G_ACMUX_PRIM_LOD_FRAC":6}
            def mux_value(value: str) -> int:
                # GetCombineLERPValue uses strncmp, so recognized names may carry suffixes.
                return next((v for name, v in mux.items() if value.startswith(name)), 6)
            vals = [mux_value(a.get(k,"")) for k in ("A0","B0","C0","D0","Aa0","Ab0","Ac0","Ad0","A1","B1","C1","D1","Aa1","Ab1","Ac1","Ad1")]
            w0 = 0xFC000000 | ((vals[0]&15)<<20) | ((vals[2]&31)<<15) | ((vals[4]&7)<<12) | ((vals[6]&7)<<9) | ((vals[8]&15)<<5) | (vals[10]&31)
            w1 = ((vals[1]&15)<<28) | ((vals[3]&7)<<15) | ((vals[5]&7)<<12) | ((vals[7]&7)<<9) | ((vals[9]&15)<<24) | ((vals[12]&7)<<21) | ((vals[14]&7)<<18) | ((vals[11]&7)<<6) | ((vals[13]&7)<<3) | (vals[15]&7)
            out.append((w0,w1))
        elif tag in ("SetGeometryMode","ClearGeometryMode"):
            bits={"G_SHADE":0x4,"G_LIGHTING":0x20000,"G_SHADING_SMOOTH":0x200000,"G_ZBUFFER":1,"G_TEXTURE_GEN":0x40000,"G_TEXTURE_GEN_LINEAR":0x80000,"G_CULL_BACK":0x400,"G_CULL_FRONT":0x200,"G_CULL_BOTH":0x600,"G_FOG":0x10000,"G_CLIPPING":0}
            mask=0
            for k,v in bits.items():
                if _has(e,k): mask |= v
            # libultraship's Wii U build uses F3DEX2 GeometryMode.
            out.append(_cmd(0xD9, (~(mask if tag=="ClearGeometryMode" else 0)) & 0xFFFFFF, 0 if tag=="ClearGeometryMode" else mask))
        elif tag == "SetCycleType":
            val=0
            for k,v in {"G_CYC_1CYCLE":0,"G_CYC_2CYCLE":0x100000,"G_CYC_COPY":0x200000,"G_CYC_FILL":0x300000}.items():
                if _has(e,k): val |= v
            out.append(_cmd(0xE3,0x0A01,val))
        elif tag == "PipelineMode":
            val=0
            for k,v in {"G_PM_1PRIMITIVE":0x800000,"G_PM_NPRIMITIVE":0}.items():
                if _has(e,k): val |= v
            out.append(_cmd(0xE3,0x0800,val))
        elif tag == "SetRenderMode":
            modes={"G_RM_ZB_OPA_SURF":0x00442230,"G_RM_AA_ZB_OPA_SURF":0x00442078,"G_RM_AA_ZB_OPA_DECAL":0x00442D58,"G_RM_AA_ZB_OPA_INTER":0x00442478,"G_RM_AA_ZB_TEX_EDGE":0x00443078,"G_RM_AA_ZB_XLU_SURF":0x004049D8,"G_RM_AA_ZB_XLU_DECAL":0x00404DD8,"G_RM_AA_ZB_XLU_INTER":0x004045D8,"G_RM_FOG_SHADE_A":0xC8000000,"G_RM_FOG_PRIM_A":0xC4000000,"G_RM_PASS":0x0C080000,"G_RM_ADD":0x04484340,"G_RM_NOOP":0,"G_RM_ZB_OPA_DECAL":0x00442E10,"G_RM_ZB_XLU_SURF":0x00404A50,"G_RM_ZB_XLU_DECAL":0x00404E50,"G_RM_OPA_SURF":0x0C084000,"G_RM_ZB_CLD_SURF":0x00404B50,"G_RM_ZB_OPA_SURF2":0x00112230,"G_RM_AA_ZB_OPA_SURF2":0x00112078,"G_RM_AA_ZB_OPA_DECAL2":0x00112D58,"G_RM_AA_ZB_OPA_INTER2":0x00112478,"G_RM_AA_ZB_TEX_EDGE2":0x00113078,"G_RM_AA_ZB_XLU_SURF2":0x001049D8,"G_RM_AA_ZB_XLU_DECAL2":0x00104DD8,"G_RM_AA_ZB_XLU_INTER2":0x001045D8,"G_RM_ADD2":0x01124340,"G_RM_ZB_OPA_DECAL2":0x00112E10,"G_RM_ZB_XLU_SURF2":0x00104A50,"G_RM_ZB_XLU_DECAL2":0x00104E50,"G_RM_ZB_CLD_SURF2":0x00104B50}
            val=modes.get(e.get("Mode1",""),0)|modes.get(e.get("Mode2",""),0)
            # gsDPSetRenderMode writes the 29-bit render-mode field (Sft=3).
            out.append(_cmd(0xE2,0x001C,val))
        elif tag == "SetOtherMode":
            names={"G_SETOTHERMODE_H":0xE3,"G_SETOTHERMODE_L":0xE2}
            flags={"G_AD_PATTERN":0x0,"G_AD_NOTPATTERN":0x10,"G_AD_DISABLE":0x30,"G_AD_NOISE":0x20,"G_CD_MAGICSQ":0,"G_CD_BAYER":0x40,"G_CD_NOISE":0x80,"G_CK_NONE":0,"G_CK_KEY":0x100,"G_TC_CONV":0,"G_TC_FILTCONV":0xA00,"G_TC_FILT":0xC00,"G_TF_POINT":0,"G_TF_AVERAGE":0x3000,"G_TF_BILERP":0x2000,"G_TL_TILE":0,"G_TL_LOD":0x10000,"G_TD_CLAMP":0,"G_TD_SHARPEN":0x20000,"G_TD_DETAIL":0x40000,"G_TP_NONE":0,"G_TP_PERSP":0x80000,"G_CYC_1CYCLE":0,"G_CYC_COPY":0x200000,"G_CYC_FILL":0x300000,"G_CYC_2CYCLE":0x100000,"G_PM_1PRIMITIVE":0x800000,"G_PM_NPRIMITIVE":0,"G_ZS_PIXEL":0,"G_ZS_PRIM":0x4}
            # Match the exact F3DEX2 constants used by DisplayListFactory.cpp.
            # Cycle-1/cycle-2 render modes are distinct; fog/pass flags are
            # independently ORed by the XML factory.
            flags.update({
                "G_RM_FOG_SHADE_A":0xC8000000,"G_RM_FOG_PRIM_A":0xC4000000,"G_RM_PASS":0x0C080000,
                "G_RM_AA_ZB_DEC_LINE":0x00407F58,"G_RM_AA_ZB_DEC_LINE2":0x00107F58,
                "G_RM_AA_ZB_OPA_DECAL":0x00442D58,"G_RM_AA_ZB_OPA_DECAL2":0x00112D58,
                "G_RM_AA_ZB_OPA_INTER":0x00442478,"G_RM_AA_ZB_OPA_INTER2":0x00112478,
                "G_RM_AA_ZB_OPA_SURF":0x00442078,"G_RM_AA_ZB_OPA_SURF2":0x00112078,
                "G_RM_AA_ZB_OPA_TERR":0x00402078,"G_RM_AA_ZB_OPA_TERR2":0x00102078,
                "G_RM_AA_ZB_PCL_SURF":0x0040007B,"G_RM_AA_ZB_PCL_SURF2":0x0010007B,
                "G_RM_AA_ZB_SUB_SURF":0x00442278,"G_RM_AA_ZB_SUB_SURF2":0x00112278,
                "G_RM_AA_ZB_SUB_TERR":0x00402278,"G_RM_AA_ZB_SUB_TERR2":0x00102278,
                "G_RM_AA_ZB_TEX_EDGE":0x00443078,"G_RM_AA_ZB_TEX_EDGE2":0x00113078,
                "G_RM_AA_ZB_TEX_INTER":0x00443478,"G_RM_AA_ZB_TEX_INTER2":0x00113478,
                "G_RM_AA_ZB_TEX_TERR":0x00403078,"G_RM_AA_ZB_TEX_TERR2":0x00103078,
                "G_RM_AA_ZB_XLU_DECAL":0x00404DD8,"G_RM_AA_ZB_XLU_DECAL2":0x00104DD8,
                "G_RM_AA_ZB_XLU_INTER":0x004045D8,"G_RM_AA_ZB_XLU_INTER2":0x001045D8,
                "G_RM_AA_ZB_XLU_LINE":0x00407858,"G_RM_AA_ZB_XLU_LINE2":0x00107858,
                "G_RM_AA_ZB_XLU_SURF":0x004049D8,"G_RM_AA_ZB_XLU_SURF2":0x001049D8,
            })
            val=0
            for k,v in flags.items():
                if _has(e,k): val |= v
            cmd=names.get(a.get("Cmd",""),0); sft=_i(e,"Sft"); ln=_i(e,"Length")
            out.append(_cmd(cmd, ((32-sft-ln)<<8)|(ln-1), val))
        elif tag == "SetTile":
            fmt=_enum(e,"Format",{"G_IM_FMT_RGBA":0,"G_IM_FMT_YUV":1,"G_IM_FMT_CI":2,"G_IM_FMT_IA":3,"G_IM_FMT_I":4},0)
            siz=_enum(e,"Size",{"G_IM_SIZ_4b":0,"G_IM_SIZ_8b":1,"G_IM_SIZ_16b":2,"G_IM_SIZ_32b":3,"G_IM_SIZ_4b_LOAD_BLOCK":2,"G_IM_SIZ_8b_LOAD_BLOCK":2,"G_IM_SIZ_16b_LOAD_BLOCK":2,"G_IM_SIZ_32b_LOAD_BLOCK":3,"G_IM_SIZ_DD":5},3)
            cm=lambda k: 1 if e.get(k)=="G_TX_MIRROR" else 2 if e.get(k)=="G_TX_CLAMP" else 0
            cmt=cm("Cmt0")|cm("Cmt1"); cms=cm("Cms0")|cm("Cms1")
            # These widths follow gsDPSetTile's _SHIFTL fields in gbi.h.
            w0=0xF5000000|((fmt&7)<<21)|((siz&3)<<19)|((_i(e,"Line")&0x1ff)<<9)|(_i(e,"TMem")&0x1ff)
            w1=((_i(e,"Tile")&7)<<24)|((_i(e,"Palette")&0xf)<<20)|((cmt&3)<<18)|((_i(e,"MaskT")&0xf)<<14)|((_i(e,"ShiftT")&0xf)<<10)|((cms&3)<<8)|((_i(e,"MaskS")&0xf)<<4)|(_i(e,"ShiftS")&0xf)
            out.append((w0,w1))
        elif tag in ("SetTileSize","LoadTile"):
            op=0xF2 if tag=="SetTileSize" else 0xF4
            w0=(op<<24)|(_i(e,"Uls")<<12)|_i(e,"Ult")
            w1=(_i(e,"T")<<24)|(_i(e,"Lrs")<<12)|_i(e,"Lrt")
            out.append((w0,w1))
        elif tag == "LoadBlock":
            lrs=min(_i(e,"Lrs") & 0xFFFFFFFF, 0xFFF)
            out.append((0xF3000000|((_i(e,"Uls")&0xfff)<<12)|(_i(e,"Ult")&0xfff),((_i(e,"Tile")&7)<<24)|((lrs&0xfff)<<12)|(_i(e,"Dxt")&0xfff)))
        elif tag == "SetTextureLUT":
            val=_enum(e,"Mode",{"G_TT_NONE":0,"G_TT_RGBA16":0x8000,"G_TT_IA16":0xC000},0)
            out.append(_cmd(0xE3,(32-14-2)<<8|1,val))
        elif tag == "LoadTLUTCmd":
            out.append((0xF0000000,(_i(e,"Tile")<<24)|(_i(e,"Count")<<14)))
        elif tag == "Triangle1":
            # XML factory deliberately replaces the OTR triangle macro's operands with raw vertex indexes.
            out.append(_cmd(0x26,_i(e,"V00"),(_i(e,"V01")<<16)|_i(e,"V02")))
        elif tag == "Triangles2":
            def tri(x,y,z): return ((x*2)<<16)|((y*2)<<8)|(z*2)
            out.append(_cmd(0x06,tri(_i(e,"V00"),_i(e,"V01"),_i(e,"V02")),tri(_i(e,"V10"),_i(e,"V11"),_i(e,"V12"))))
        elif tag == "CullDisplayList": out.append((0x03000000|(_i(e,"Start")*2),_i(e,"End")*2))
        elif tag in ("CallDisplayList", "JumpToDisplayList", "BranchToDisplayList"):
            path=e.get("Path","")
            if path.startswith(">0x") or path.startswith(">0X"):
                push=0 if tag=="CallDisplayList" else 1
                out.append((0xDE000000|(push<<16),int(path[1:],16)|1))
            else:
                h=reference_hash(path,index)
                low=0 if tag=="CallDisplayList" else 0x10000
                out.extend([_cmd(0x31,low,0),((h>>32)&0xffffffff,h&0xffffffff)])
        elif tag == "Matrix":
            path=e.get("Path","")
            param=e.get("Param","")
            param_value={"G_MTX_PUSH":0x01,"G_MTX_NOPUSH":0,"G_MTX_LOAD":0x02,"G_MTX_MUL":0,"G_MTX_MODELVIEW":0,"G_MTX_PROJECTION":0x04}.get(param,0)
            low=(7<<19)|(param_value^0x01)
            if path.startswith(">0x"):
                out.append(_cmd(0xDA,low,int(path[1:],16)|1))
            else:
                h=reference_hash(path,index)
                out.extend([_cmd(0x36,low,0),((h>>32)&0xffffffff,h&0xffffffff)])
        elif tag == "LoadVertices":
            h=reference_hash(e.get("Path",""),index)
            count=_i(e,"Count"); v0=_i(e,"VertexBufferIndex")
            out.extend([((0x32<<24)|((count&0xff)<<12)|(((v0+count)&0x7f)<<1),_i(e,"VertexOffset")*16),((h>>32)&0xffffffff,h&0xffffffff)])
        elif tag == "SetTextureImage":
            path=e.get("Path","")
            fmt=_enum(e,"Format",{"G_IM_FMT_RGBA":0,"G_IM_FMT_YUV":1,"G_IM_FMT_CI":2,"G_IM_FMT_IA":3,"G_IM_FMT_I":4},0)
            # gbi.h: both *_LOAD_BLOCK sizes are G_IM_SIZ_16b and G_IM_SIZ_DD is 5; any other name keeps the
            # factory's G_IM_SIZ_32b default.
            siz=_enum(e,"Size",{"G_IM_SIZ_4b":0,"G_IM_SIZ_8b":1,"G_IM_SIZ_16b":2,"G_IM_SIZ_16b_LOAD_BLOCK":2,"G_IM_SIZ_8b_LOAD_BLOCK":2,"G_IM_SIZ_32b":3,"G_IM_SIZ_DD":5},3)
            if path[:3] in (">0x", ">0X"):
                # Segment address: the XML factory emits a plain G_SETTIMG with the address | 1.
                w0=(0xFD<<24)|((fmt&7)<<21)|((siz&3)<<19)|((_i(e,"Width")-1)&0xfff)
                out.append((w0,(int(path[1:],16)|1)&0xffffffff))
            else:
                try:
                    _,h=resolve_path(path,index)
                except ValueError:
                    # The texture exists in no archive (a pack bug). The XML filepath handler then leaves the
                    # texture state unchanged; a dangling hash would do the same but log an error on every draw.
                    # Emit a PipeSync, the XML factory's own no-op fallback.
                    UNRESOLVED_TEXTURES.add(path)
                    out.append(_cmd(0xE7))
                else:
                    w0=(0x20<<24)|((fmt&7)<<21)|((siz&3)<<19)|((_i(e,"Width")-1)&0xfff)
                    out.extend([(w0,0),((h>>32)&0xffffffff,h&0xffffffff)])
            # The XML factory explicitly appends a PipeSync after SetTextureImage.
            out.append(_cmd(0xE7))
        elif tag == "GeometryFlags":
            # Not recognized by the game's XML factory: its defined fallback is PipeSync.
            out.append(_cmd(0xE7))
        else:
            # Exactly mirrors ResourceFactoryXMLDisplayListV0's unknown-node fallback.
            out.append(_cmd(0xE7))
    ucode = 4  # ucode_f3dex2, the Wii U build's F3DEX2 GBI selection
    body = bytes([ucode]) + b"\xff" * 7 + pack_words(out)
    return (header if header is not None else dl_header()) + body


def _archive_header(path: Path, kind: str) -> bytes:
    wanted = b"TLDO" if kind == "DisplayList" else b"XTVO"
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            with archive.open(info) as f:
                header = f.read(64)
            if len(header) == 64 and header[4:8] == wanted:
                return header
        if kind == "Vertex":
            for info in archive.infolist():
                with archive.open(info) as f:
                    header = f.read(64)
                if len(header) == 64 and header[4:8] == b"TLDO":
                    return header[:4] + b"XTVO" + header[8:]
    raise ValueError(f"no binary {kind} resource header found in {path}")


@cache
def _header_sample(kind: str) -> bytes:
    raise ValueError(f"no {kind} header supplied; pass a header from the user's O2R")


def dl_header() -> bytes:
    return _header_sample("DisplayList")


def vertex(data: bytes, member: str, header: bytes | None = None) -> bytes:
    root=ET.fromstring(data)
    if root.tag!="Vertex": raise ValueError(f"{member}: expected Vertex XML")
    rows=[]
    for e in root:
        if e.tag!="Vtx": continue
        s16=lambda value: ((value + 0x8000) & 0xffff) - 0x8000
        rows.append(struct.pack("<hhhHhhBBBB",s16(_i(e,"X")),s16(_i(e,"Y")),s16(_i(e,"Z")),0,s16(_i(e,"S")),s16(_i(e,"T")),_i(e,"R")&255,_i(e,"G")&255,_i(e,"B")&255,_i(e,"A")&255))
    return (header if header is not None else _header_sample("Vertex")) + struct.pack("<I",len(rows)) + b"".join(rows)


def convert(source: Path, destination: Path) -> tuple[int,int,list[str]]:
    indexes=loaded_members(source)
    headers = (_archive_header(source, "DisplayList"), _archive_header(source, "Vertex"))
    destination.parent.mkdir(parents=True,exist_ok=True)
    converted=0; unchanged=0
    fd,tmp=tempfile.mkstemp(prefix=destination.name+".",suffix=".tmp",dir=destination.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(source,"r") as src, zipfile.ZipFile(tmp,"w") as dst:
            dst.comment=src.comment
            for info in src.infolist():
                data=src.read(info.filename)
                if data.lstrip().startswith(b"<"):
                    try: root=ET.fromstring(data)
                    except ET.ParseError: root=None
                    if root is not None and root.tag in ("DisplayList","Vertex"):
                        data=display_list(data,indexes,info.filename,headers[0]) if root.tag=="DisplayList" else vertex(data,info.filename,headers[1])
                        converted+=1
                    else: unchanged+=1
                else: unchanged+=1
                dst.writestr(info,data,compress_type=info.compress_type)
        os.replace(tmp,destination)
    except BaseException:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
        raise
    return converted,unchanged,[]


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input",type=Path)
    parser.add_argument("output",type=Path)
    args=parser.parse_args()
    try:
        converted,unchanged,_=convert(args.input,args.output)
    except Exception as exc:
        parser.exit(2,f"error: {exc}\n")
    print(f"converted={converted} unchanged={unchanged} output={args.output}")
    for path in sorted(UNRESOLVED_TEXTURES):
        print(f"unresolved texture -> PipeSync: {path}")
    for path in sorted(UNRESOLVED_REFERENCES):
        print(f"unresolved reference -> raw-path hash: {path}")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
