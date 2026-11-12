"""
DualRip CLI launcher (PyInstaller entry point).
"""

import sys

from dualrip.cli import main

if __name__ == '__main__':
    sys.exit(main())
