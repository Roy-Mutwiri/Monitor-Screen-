# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: builds two executables into dist/StudioMonitor/.

    StudioMonitor.exe        windowed GUI
    studio-monitor-cli.exe   console CLI (list-windows, select, run, calibrate, ...)

Build with:  python packaging/build_exe.py
"""
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_dynamic_libs

ROOT = os.path.abspath(os.path.join(os.path.dirname(SPEC), ".."))
SRC = os.path.join(ROOT, "src")

hidden = ["studio_monitor.gui.app", "PIL.ImageTk"]
datas = [(os.path.join(ROOT, "rules", name), "rules") for name in ("studio_rules.json", "live_state_rules.json")]
binaries = []
for pkg in ("winrt", "winocr", "windows_capture", "numpy", "ttkbootstrap"):
    try:
        hidden += collect_submodules(pkg)
        datas += collect_data_files(pkg)
        binaries += collect_dynamic_libs(pkg)
    except Exception:
        pass

a = Analysis(
    [os.path.join(ROOT, "packaging", "entry_gui.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["matplotlib", "numpy.testing", "scipy"],
    noarchive=False,
)
b = Analysis(
    [os.path.join(ROOT, "packaging", "entry_cli.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["matplotlib", "numpy.testing", "scipy"],
    noarchive=False,
)
MERGE((a, "entry_gui", "StudioMonitor"), (b, "entry_cli", "studio-monitor-cli"))

pyz_a = PYZ(a.pure)
exe_a = EXE(pyz_a, a.scripts, [], exclude_binaries=True, name="StudioMonitor", console=False,
            disable_windowed_traceback=False, icon=None)
pyz_b = PYZ(b.pure)
exe_b = EXE(pyz_b, b.scripts, [], exclude_binaries=True, name="studio-monitor-cli", console=True, icon=None)

coll = COLLECT(exe_a, a.binaries, a.datas, exe_b, b.binaries, b.datas, strip=False, upx=False, name="StudioMonitor")
