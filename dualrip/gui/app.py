"""DualRip GUI entry point."""

import os
import sys

from PySide6.QtWidgets import QApplication

from .main_window import MainWindow


def _icon_path():
    # packaged (PyInstaller) layout first, then repository layout
    if getattr(sys, '_MEIPASS', None):
        p = os.path.join(sys._MEIPASS, 'icon.ico')
        if os.path.exists(p):
            return p
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, '..', '..', 'icon.ico'))


def main(argv=None):
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName('DualRip')
    app.setOrganizationName('DualRip')
    win = MainWindow(icon_path=_icon_path())
    win.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
