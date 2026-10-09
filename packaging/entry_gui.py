"""PyInstaller entry point for the windowed GUI executable."""
import sys

from studio_monitor.win32.api import enable_dpi_awareness

enable_dpi_awareness()
from studio_monitor.cli import main  # noqa: E402

sys.exit(main(["gui"] if len(sys.argv) == 1 else sys.argv[1:]))
