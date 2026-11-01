from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEasingCurve, Property, QPropertyAnimation, QRectF, QTimer, Signal, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QAbstractButton, QSizePolicy, QToolButton, QWidget

from icons import line_icon


def fitted_rect(image: QPixmap, bounds: QRectF) -> QRectF:
    if image.isNull():
        return QRectF()
    scale = min(bounds.width() / image.width(), bounds.height() / image.height())
    width, height = image.width() * scale, image.height() * scale
    return QRectF(bounds.center().x() - width / 2, bounds.center().y() - height / 2, width, height)


def icon_button(name: str, title: str, parent=None) -> QToolButton:
    widget = QToolButton(parent)
    widget.setObjectName("iconButton")
    widget.setIcon(line_icon(name, "#273344"))
    widget.setToolTip(title)
    widget.setAccessibleName(title)
    widget.setFixedSize(48, 48)
    widget.setCursor(Qt.PointingHandCursor)
    return widget


class CoverCanvas(QWidget):
    stepped = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.image = QPixmap()
        self.previous = QPixmap()
        self.image_path = None
        self._opacity = 1.0
        self._pressed = None
        self.setMinimumSize(240, 260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("风格效果预览")
        self.previous_button = icon_button("arrow-left", "上一个风格", self)
        self.next_button = icon_button("arrow-right", "下一个风格", self)
        self.previous_button.clicked.connect(lambda: self.stepped.emit(-1))
        self.next_button.clicked.connect(lambda: self.stepped.emit(1))
        self.transition = QPropertyAnimation(self, b"opacity", self)
        self.transition.setDuration(180)
        self.transition.setEasingCurve(QEasingCurve.InOutSine)
        self.transition.finished.connect(self._clear_previous)

    def _get_opacity(self) -> float:
        return self._opacity

    def _set_opacity(self, value: float) -> None:
        self._opacity = value
        self.update()

    opacity = Property(float, _get_opacity, _set_opacity)

    def _clear_previous(self) -> None:
        self.previous = QPixmap()

    def set_cover(self, path: Path) -> None:
        if path == self.image_path:
            return
        self.transition.stop()
        self.previous = self.image
        self.image = QPixmap(str(path))
        self.image_path = path
        if self.previous.isNull() or not self.isVisible():
            self._opacity = 1.0
            self.previous = QPixmap()
            self.update()
        else:
            self.transition.setStartValue(0.0)
            self.transition.setEndValue(1.0)
            self.transition.start()

    def image_rect(self) -> QRectF:
        return fitted_rect(self.image, QRectF(self.rect()))

    def _paint_picture(self, painter: QPainter, picture: QPixmap, opacity: float) -> None:
        if picture.isNull():
            return
        target = fitted_rect(picture, QRectF(self.rect()))
        clip = QPainterPath()
        clip.addRoundedRect(target, 6, 6)
        painter.save()
        painter.setOpacity(opacity)
        painter.setClipPath(clip)
        painter.drawPixmap(target, picture, QRectF(picture.rect()))
        painter.restore()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor("#eef0f4"))
        self._paint_picture(painter, self.previous, 1.0 - self._opacity)
        self._paint_picture(painter, self.image, self._opacity)
        painter.end()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        top = (self.height() - 48) // 2
        self.previous_button.move(12, top)
        self.next_button.move(self.width() - 60, top)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._pressed = event.position()
            self.setFocus(Qt.MouseFocusReason)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._pressed is not None:
            delta = event.position() - self._pressed
            self._pressed = None
            if abs(delta.x()) >= 50 and abs(delta.x()) > abs(delta.y()) * 1.5:
                self.stepped.emit(1 if delta.x() < 0 else -1)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Left, Qt.Key_Right):
            self.stepped.emit(-1 if event.key() == Qt.Key_Left else 1)
            event.accept()
            return
        super().keyPressEvent(event)


class CoverThumbnail(QAbstractButton):
    def __init__(self, index: int, name: str, path: Path) -> None:
        super().__init__()
        self.index = index
        self.preview = QPixmap(str(path)).scaled(180, 220, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.setCheckable(True)
        self.setFixedHeight(104)
        self.setMinimumWidth(44)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setToolTip(name)
        self.setAccessibleName(name)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        painter.setOpacity(1.0 if self.isEnabled() else 0.4)
        painter.setPen(QPen(QColor("#2860d9" if self.isChecked() else "#dfe3eb"), 2 if self.isChecked() else 1))
        painter.setBrush(QColor("#eef3ff" if self.isChecked() else "#ffffff"))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)
        photo = fitted_rect(self.preview, QRectF(6, 6, self.width() - 12, self.height() - 30))
        painter.drawPixmap(photo, self.preview, QRectF(self.preview.rect()))
        painter.setPen(QColor("#2860d9" if self.isChecked() else "#828b9a"))
        painter.setFont(QFont("Microsoft YaHei", 9, QFont.DemiBold))
        painter.drawText(QRectF(0, self.height() - 24, self.width(), 20), Qt.AlignCenter, f"{self.index + 1:02}")
        if self.hasFocus():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("#2860d9"), 1, Qt.DotLine))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(4, 4, -4, -4), 4, 4)
        painter.end()


class CreationSpinner(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.angle = 0
        self.setFixedSize(144, 144)
        self.timer = QTimer(self)
        self.timer.setInterval(35)
        self.timer.timeout.connect(self._advance)

    def _advance(self) -> None:
        self.angle = (self.angle + 6) % 360
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.timer.start()

    def hideEvent(self, event) -> None:
        self.timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#e0e6f1"), 3))
        painter.drawEllipse(QRectF(10, 10, 124, 124))
        painter.setPen(QPen(QColor("#2860d9"), 3, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(QRectF(10, 10, 124, 124), self.angle * 16, 95 * 16)
        painter.setPen(QPen(QColor("#63a09a"), 2, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(QRectF(24, 24, 96, 96), (180 - self.angle) * 16, 210 * 16)
        painter.setPen(QColor("#243047"))
        painter.setFont(QFont("Microsoft YaHei", 23, QFont.DemiBold))
        painter.drawText(self.rect(), Qt.AlignCenter, "AI")
        painter.end()
