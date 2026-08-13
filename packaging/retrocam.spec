# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for RetroCam Rescue — the authoritative build description.

Per-OS output mode, and why:

* **macOS -> onedir ``.app``.** ``--onefile`` combined with ``--windowed`` is
  deprecated in PyInstaller 6 and becomes a hard error in 7 ("a .app bundle can
  not be a single file"). It also clashes with macOS code-signing, and it costs
  roughly 2 s of startup on every launch because the bundle re-extracts itself
  into a temporary directory.
* **Windows -> onefile ``.exe``.** One artifact to download and double-click is
  what a non-technical Windows user needs, and Windows has no bundle format to
  respect. The costs are accepted knowingly: a slower first start (it unpacks
  into %TEMP%\\_MEIxxxxxx) and a higher chance of an antivirus false positive.
  If Defender ever starts flagging it, switch IS_WIN to the onedir branch and
  ship a ZIP — the rest of this file does not need to change.

Build with:
    pyinstaller packaging/retrocam.spec --noconfirm --clean
"""

import sys
from pathlib import Path

APP_NAME = "RetroCam Rescue"
BUNDLE_ID = "org.retrocam.rescue"
VERSION = "0.1.0"  # keep in sync with src/retrocam/__init__.py and version_info.txt

# SPECPATH is injected by PyInstaller and points at packaging/, so the repo root
# is its parent. Deriving it this way keeps the build working no matter which
# directory pyinstaller was invoked from.
ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - injected by PyInstaller
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

ICON = str(ROOT / "assets" / ("icon.icns" if IS_MAC else "icon.ico"))
if not Path(ICON).exists():
    ICON = None  # building the app without artwork must still work

VERSION_FILE = ROOT / "packaging" / "version_info.txt"

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "retrocam_gui.py")],
    pathex=[str(ROOT / "src")],  # src layout; works for an editable install too
    binaries=[],
    datas=[],  # the core is stdlib-only: there are no data files to carry
    # registry.py imports the backends statically, so PyInstaller's module graph
    # finds them by itself. Listed anyway as insurance against ALL_BACKENDS ever
    # becoming lazy — a frozen build with a silently empty backend list would
    # report "no camera found" on a machine with a camera plugged in.
    # retrocam.app is here because __main__ imports it inside a function, which
    # the static analysis does not always follow.
    hiddenimports=[
        "retrocam.app",
        "retrocam.i18n",
        "retrocam.backends.massstorage",
        "retrocam.backends.gphoto2_backend",
        "retrocam.backends.wia",
    ],
    excludes=[
        # Build-time only; shipping them just inflates the download.
        "pip",
        "setuptools",
        "wheel",
        "pydoc_data",
        "test",
        "tkinter.test",
        "lib2to3",
        # Never let a scientific/plotting stack ride along because something
        # transitively touched it.
        "numpy",
        "matplotlib",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

if IS_WIN:
    # ---- onefile: binaries and datas are embedded in the EXE ----
    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=APP_NAME,
        console=False,  # GUI app: no console window behind the Tk window
        disable_windowed_traceback=False,
        icon=ICON,
        version=str(VERSION_FILE) if VERSION_FILE.exists() else None,
        # UPX compression is the single biggest cause of antivirus false
        # positives on PyInstaller output, and saves a few MB at most.
        upx=False,
        strip=False,
    )
else:
    # ---- onedir: the EXE is a small launcher, COLLECT gathers the rest ----
    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        console=False,
        icon=ICON,
        upx=False,
        strip=False,
        # Native architecture, on purpose. `target_arch="universal2"` needs a
        # universal2 CPython *and* a universal2 wheel for every binary
        # dependency; uv-managed (python-build-standalone) and Homebrew pythons
        # are single-arch, and Pillow no longer publishes universal2 wheels. So
        # a universal2 build here would either fail outright or silently drop a
        # slice. We ship one artifact per architecture instead, and CI reads the
        # architecture back out of the built binary with `lipo -archs` and fails
        # the release if it does not match the name on the file. See
        # .github/workflows/build.yml and packaging/build.md.
        target_arch=None,
        # None means PyInstaller ad-hoc signs the bundle, which is *mandatory*
        # on Apple Silicon (an unsigned arm64 binary will not execute at all).
        # It is not a Developer ID signature and it is not notarization: see
        # packaging/build.md for what the user sees on first launch.
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(  # noqa: F821
        exe,
        a.binaries,
        a.datas,
        name=APP_NAME,
        upx=False,
        strip=False,
    )

    if IS_MAC:
        app = BUNDLE(  # noqa: F821
            coll,
            name=APP_NAME + ".app",
            icon=ICON,
            bundle_identifier=BUNDLE_ID,
            version=VERSION,
            info_plist={
                "CFBundleShortVersionString": VERSION,
                "CFBundleVersion": VERSION,
                "NSHighResolutionCapable": True,
                "LSApplicationCategoryType": "public.app-category.utilities",
                # 11.0 (Big Sur) for BOTH architectures, deliberately. arm64
                # macOS starts at 11.0 anyway, and while the Intel build's real
                # deployment target may well be lower, nobody has verified that
                # on a Mac older than Big Sur. Declaring a floor we have not
                # tested would trade a clear "requires macOS 11" refusal for an
                # obscure dyld crash on launch. CI logs the binary's actual
                # minimum (`vtool -show-build`) so this can be lowered later on
                # evidence rather than on optimism.
                "LSMinimumSystemVersion": "11.0",
                "NSHumanReadableCopyright": "MIT licensed. No data leaves this machine.",
                # macOS 10.15+ TCC: without these strings the system *silently
                # denies* access instead of prompting, which would look exactly
                # like "the card is empty". The app reads removable volumes and
                # writes into whichever folder the user picks.
                "NSRemovableVolumesUsageDescription":
                    "RetroCam Rescue reads photos from your camera or memory card.",
                "NSDownloadsFolderUsageDescription":
                    "RetroCam Rescue saves the rescued photos into your Downloads folder.",
                "NSDocumentsFolderUsageDescription":
                    "RetroCam Rescue can save the rescued photos into a folder you choose.",
                "NSDesktopFolderUsageDescription":
                    "RetroCam Rescue can save the rescued photos into a folder you choose.",
            },
        )
