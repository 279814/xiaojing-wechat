"""PyInstaller entry.

The client is normally started with ``python -m app.main``. The frozen executable
must import it the same way so ``app`` stays a package and relative imports work.

``Autosale.exe --self-check`` writes self_check.json next to the exe and exits
without opening a window.

``Autosale.exe --dry-run`` moves the fake cursor over the send targets without
clicking or typing, writes the coordinates to dry-run.txt next to the exe, and exits.
"""

import sys

from app.packaged_runtime import configure_logging, run_dry_run, run_self_check

configure_logging()

if "--self-check" in sys.argv[1:]:
    raise SystemExit(run_self_check())

if "--dry-run" in sys.argv[1:]:
    raise SystemExit(run_dry_run())

from app.main import main

raise SystemExit(main())
