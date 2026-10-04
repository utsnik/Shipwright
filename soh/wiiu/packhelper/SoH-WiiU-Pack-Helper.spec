from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


HERE = Path(SPECPATH)

numpy_datas, numpy_binaries, numpy_hidden = collect_all("numpy")
pillow_datas, pillow_binaries, pillow_hidden = collect_all("PIL")

datas = [*numpy_datas, *pillow_datas]
binaries = [*numpy_binaries, *pillow_binaries]
hiddenimports = [
    *numpy_hidden,
    *pillow_hidden,
    *collect_submodules("tkinter"),
    "tkinter",
    "tkinter.ttk",
    "soh_wiiu_packtool",
    "o2r_bc_convert",
    "o2r_downscale",
    "o2r_xml_to_binary",
    "soh_fix_blank_skyboxes",
]

a = Analysis(
    [str(HERE / "packhelper.py")],
    pathex=[str(HERE)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SoH-WiiU-Pack-Helper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
