from __future__ import annotations

import sys


def main() -> int:
    from .win32.api import enable_dpi_awareness
    enable_dpi_awareness()
    from .cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
