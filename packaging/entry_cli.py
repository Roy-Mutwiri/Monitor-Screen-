"""PyInstaller entry point for the console executable."""
import sys

from studio_monitor.win32.api import enable_dpi_awareness

enable_dpi_awareness()
from studio_monitor.cli import main  # noqa: E402

sys.exit(main())
