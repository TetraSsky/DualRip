"""
DualRip GUI launcher (PyInstaller entry point).
"""

import sys

from dualrip.gui.app import main

if __name__ == '__main__':
    sys.exit(main())
