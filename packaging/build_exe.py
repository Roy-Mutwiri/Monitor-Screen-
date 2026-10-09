"""Build the Windows executables with PyInstaller.

    python packaging/build_exe.py            -> dist/StudioMonitor/StudioMonitor.exe (+ CLI exe)
    python packaging/build_exe.py --zip      -> also dist/StudioMonitor-<version>-win64.zip
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from studio_monitor import __version__  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", action="store_true")
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()
    dist, build = ROOT / "dist", ROOT / "build"
    if args.clean:
        shutil.rmtree(dist, ignore_errors=True)
        shutil.rmtree(build, ignore_errors=True)
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--distpath", str(dist), "--workpath", str(build), str(ROOT / "packaging" / "studio_monitor.spec")]
    print(" ".join(cmd))
    rc = subprocess.call(cmd, cwd=ROOT)
    if rc != 0:
        return rc
    out = dist / "StudioMonitor"
    print(f"built: {out / 'StudioMonitor.exe'}")
    if args.zip:
        archive = shutil.make_archive(str(dist / f"StudioMonitor-{__version__}-win64"), "zip", root_dir=dist, base_dir="StudioMonitor")
        print(f"zip: {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
