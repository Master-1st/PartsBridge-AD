"""PyInstaller entry point for the Windows desktop release."""

import sys


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # The packaged build can be verified without opening or driving a GUI.
        from lcsc_altium_loader.cli import main
        raise SystemExit(main())
    else:
        from lcsc_altium_loader.gui import run
        run()
