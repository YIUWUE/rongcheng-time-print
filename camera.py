from __future__ import annotations

import os

from PySide6.QtCore import QObject, QRectF, QSize, Signal, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QTransform
from PySide6.QtMultimedia import QCamera, QMediaCaptureSession, QMediaDevices, QVideoSink
from PySide6.QtWidgets import QWidget

from paths import log


def preview_source_rect(image_size: QSize, viewport_size: QSize) -> QRectF:
    source = QRectF(0, 0, image_size.width(), image_size.height())
    if source.isEmpty() or viewport_size.isEmpty():
        return source
    ratio = viewport_size.width() / viewport_size.height()
    if source.width() / source.height() > ratio:
        width = source.height() * ratio
        source.setLeft((source.width() - width) / 2)
        source.setWidth(width)
    else:
        height = source.width() / ratio
        source.setTop((source.height() - height) / 2)
        source.setHeight(height)
    return source


class CameraController(QObject):
    frame_ready = Signal(QImage)
    status = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.camera = None
        self.session = QMediaCaptureSession(self)
        self.sink = QVideoSink(self)
        self.session.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._frame)
        self.last_frame = QImage()
        self.format_text = "未连接"

    def start(self) -> None:
        if self.camera is not None:
            self.camera.start()
            return
        devices = QMediaDevices.videoInputs()
        if not devices:
            self.status.emit("未找到摄像头，请连接设备后重试。")
            return
        device = QMediaDevices.defaultVideoInput()
        self.camera = QCamera(device, self)
        formats = device.videoFormats()
        if formats:
            def score(fmt):
                size = fmt.resolution()
                long_edge, short_edge = sorted((size.width(), size.height()), reverse=True)
                distance = abs(long_edge - 1920) + abs(short_edge - 1080)
                return (250000 if (long_edge, short_edge) == (1920, 1080) else 0) + (60000 if abs(fmt.maxFrameRate() - 30) <= 1 else 0) - distance * 120 - max(0, 30 - fmt.maxFrameRate()) * 1000
            best = max(formats, key=score)
            self.camera.setCameraFormat(best)
            size = best.resolution()
            self.format_text = f"{size.width()} × {size.height()} · {best.maxFrameRate():.0f} fps"
        self.camera.errorOccurred.connect(lambda error, text: self.status.emit(text or "摄像头连接异常，请重试。"))
        self.session.setCamera(self.camera)
        self.last_frame = QImage()
        self.status.emit("摄像头准备中")
        self.camera.start()
        log(f"camera format={self.format_text}")

    def _frame(self, frame) -> None:
        image = frame.toImage()
        if image.isNull():
            return
        try:
            rotation = int(os.environ.get("RONGCHENG_CAMERA_ROTATION", "0")) % 360
        except ValueError:
            rotation = 0
        if rotation:
            image = image.transformed(QTransform().rotate(rotation))
        self.last_frame = image
        self.frame_ready.emit(image)

    def stop(self) -> None:
        if self.camera is not None:
            self.camera.stop()
            self.session.setCamera(None)
            self.camera.deleteLater()
            self.camera = None


class CameraPreview(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.image = QImage()
        self.review_mode = False
        self.countdown = ""
        self.setMinimumSize(200, 240)
        self.setAttribute(Qt.WA_OpaquePaintEvent)

    def set_frame(self, image: QImage) -> None:
        self.image = image
        self.review_mode = False
        self.update()

    def set_photo(self, image: QImage) -> None:
        self.image = image.copy()
        self.review_mode = True
        self.countdown = ""
        self.update()

    def capture_frame(self, image: QImage) -> QImage:
        if image.isNull():
            return QImage()
        return image.copy(preview_source_rect(image.size(), self.size()).toAlignedRect().intersected(image.rect()))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111917"))
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        if not self.image.isNull():
            if self.review_mode:
                size = self.image.size().scaled(self.size(), Qt.KeepAspectRatio)
                target = QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2,
                                size.width(), size.height())
                painter.drawImage(target, self.image)
            else:
                source = preview_source_rect(self.image.size(), self.size())
                painter.drawImage(QRectF(self.rect()), self.image, source)
        if self.countdown:
            painter.fillRect(self.rect(), QColor(0, 0, 0, 35))
            painter.setPen(Qt.white)
            painter.setFont(QFont("Microsoft YaHei", 70, QFont.Bold))
            painter.drawText(self.rect(), Qt.AlignCenter, self.countdown)
        painter.end()
