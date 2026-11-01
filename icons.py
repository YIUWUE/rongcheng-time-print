from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from paths import RESOURCE_DIR


@lru_cache(maxsize=32)
def line_icon(name: str, color: str) -> QIcon:
    source = (RESOURCE_DIR / "assets" / "icons" / f"{name}.svg").read_bytes()
    icon = QIcon()
    for mode, stroke in ((QIcon.Normal, color), (QIcon.Disabled, "#98a59c")):
        renderer = QSvgRenderer(QByteArray(source.replace(b"currentColor", stroke.encode("ascii"))))
        pixmap = QPixmap(36, 36)
        pixmap.setDevicePixelRatio(2)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter, QRectF(0, 0, 18, 18))
        painter.end()
        icon.addPixmap(pixmap, mode)
    return icon
