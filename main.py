from __future__ import annotations

import os
import sys

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication


def main() -> int:
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName("蓉城时光印")
    app.setOrganizationName("Rongcheng Time Print")
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei", 11))
    if len(sys.argv) >= 5 and sys.argv[1] == "--print-helper":
        from services import print_image
        try:
            copies = int(sys.argv[4])
        except ValueError:
            return 2
        return print_image(sys.argv[2], sys.argv[3], copies)
    from ui import BoothWindow, RotatedWindow
    inner = BoothWindow()
    windowed = "--windowed" in sys.argv or os.environ.get("RONGCHENG_WINDOWED") == "1"
    override = os.environ.get("RONGCHENG_ROTATED_KIOSK")
    screen = app.primaryScreen().availableGeometry()
    rotated = override == "1" or (override != "0" and not windowed and screen.width() > screen.height())
    window = RotatedWindow(inner) if rotated else inner
    if windowed:
        window.resize(760, min(1280, screen.height() - 60))
        window.show()
    else:
        window.showFullScreen()
    app.aboutToQuit.connect(inner.camera.stop)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
