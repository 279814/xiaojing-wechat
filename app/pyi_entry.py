"""PyInstaller entry.

The client is normally started with ``python -m app.main``. The frozen executable
must import it the same way so ``app`` stays a package and relative imports work.

``Autosale.exe --self-check`` writes self_check.json next to the exe and exits
without opening a window.
"""

import sys

from app.packaged_runtime import configure_logging, run_self_check

configure_logging()

if "--self-check" in sys.argv[1:]:
    raise SystemExit(run_self_check())

from app.main import main

raise SystemExit(main())
