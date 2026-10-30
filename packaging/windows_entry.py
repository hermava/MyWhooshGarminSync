"""Windowed executable entry point and isolated build verification."""

import sys

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--build-smoke-test":
        from mywhoosh_garmin_sync.packaging_smoke import run

        raise SystemExit(run(sys.argv[2]))
    from mywhoosh_garmin_sync.gui import main

    raise SystemExit(main())
