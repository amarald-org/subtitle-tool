# PyInstaller spec for the standalone "Subtitle Tool" app (macOS, Windows, Linux).
# Build: uv run --extra gui --extra auto --with pyinstaller --with pillow pyinstaller packaging/subtitle-tool.spec
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
NAME = "Subtitle Tool"

datas = [(str(ROOT / "src/subtitle_tool/assets"), "subtitle_tool/assets")]
datas += collect_data_files("faster_whisper")
for pkg in ("ffsubsync", "faster_whisper", "subtitle_tool", "huggingface_hub", "tokenizers", "tqdm", "rich"):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass

a = Analysis(
    [str(ROOT / "packaging/launch.py")],
    pathex=[str(ROOT / "src")],
    hookspath=[str(ROOT / "packaging/hooks")],
    binaries=collect_dynamic_libs("ctranslate2"),
    datas=datas,
    hiddenimports=collect_submodules("ffsubsync") + ["webrtcvad", "subtitle_tool.gui"],
    excludes=[
        "tkinter", "matplotlib", "IPython",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
        "PySide6.QtQuick", "PySide6.QtQml", "PySide6.Qt3DCore", "PySide6.QtCharts",
        "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtDesigner",
    ],
)
pyz = PYZ(a.pure)

icon = str(ROOT / "packaging/icon.png")  # converted to .icns / .ico by Pillow
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=NAME,
    console=False,
    icon=icon,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{NAME}.app",
        icon=icon,
        bundle_identifier="io.github.amarald-org.subtitle-tool",
        version=VERSION,
        info_plist={
            "CFBundleName": NAME,
            "CFBundleDisplayName": NAME,
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
        },
    )
