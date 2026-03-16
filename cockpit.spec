# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Claude Cockpit — single-file macOS arm64 binary."""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Textual loads CSS at runtime from its package — must be bundled
textual_datas = collect_data_files("textual")

# Collect all submodules that static analysis misses
hidden = (
    collect_submodules("textual")
    + collect_submodules("rich")
    + collect_submodules("watchfiles")
    + [
        "cockpit",
        "cockpit.app",
        "cockpit.data",
    ]
)

a = Analysis(
    ["cockpit/__main__.py"],
    pathex=["."],
    binaries=[],
    datas=textual_datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "iterm2",       # optional dependency, not needed in binary
        "tkinter",
        "unittest",
        "pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="cockpit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=False,
    console=True,
    target_arch="arm64",
)
