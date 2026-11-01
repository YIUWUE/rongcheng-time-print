from __future__ import annotations

import shutil
from pathlib import Path
from time import monotonic
from uuid import uuid4

from PySide6.QtCore import QEvent, QRect, QSize, QThread, QTimer, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractSpinBox, QFileDialog, QFrame, QGraphicsScene, QGraphicsView,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QSizePolicy, QSpinBox, QStackedWidget,
    QToolButton, QVBoxLayout, QWidget,
)

from camera import CameraController, CameraPreview
from catalog import STYLES, CityStyle
from icons import line_icon
from paths import APP_NAME, CAPTURE_SECONDS, DATA_DIR, RESOURCE_DIR, RETURN_SECONDS, SEEDREAM_MODEL, load_api_key
from rendering import HtmlRenderer, RenderJob, local_time
from services import GenerationWorker, PrintWorker, physical_printer
from ui_widgets import CoverCanvas, CoverThumbnail, CreationSpinner, icon_button

STYLE_STEP, CAMERA_STEP, RESULT_STEP = range(3)


def label(text: str, name: str, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def button(text: str, primary: bool = False, icon=None) -> QPushButton:
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    if icon is not None:
        widget.setIcon(line_icon(icon, "#ffffff" if primary else "#273344"))
        widget.setIconSize(QSize(18, 18))
    return widget


class PosterCanvas(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.image = QPixmap()
        self.setMinimumSize(240, 280)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_poster(self, path: str | None) -> None:
        self.image = QPixmap(path) if path else QPixmap()
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#eef0f4"))
        if not self.image.isNull():
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            target = self.rect().adjusted(12, 12, -12, -12)
            size = self.image.size().scaled(target.size(), Qt.KeepAspectRatio)
            rect = QRect(target.x() + (target.width() - size.width()) // 2, target.y() + (target.height() - size.height()) // 2, size.width(), size.height())
            painter.drawPixmap(rect, self.image)
        else:
            painter.setPen(QColor("#8a93a3"))
            painter.drawText(self.rect(), Qt.AlignCenter, "蓉城时光印")
        painter.end()


class BoothWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(RESOURCE_DIR / "assets" / "placeholder.svg")))
        self.resize(760, 1280)
        self.style_index = 0
        self.current_step = 0
        self.max_step = 0
        self.busy = False
        self.print_pending = False
        self.print_locked = False
        self.print_state = ""
        self.closing = False
        self.session_id = uuid4().hex
        self.timestamp = local_time()
        self.current_photo = QImage()
        self.photo_confirmed = False
        self.generated_path = None
        self.pdf_path = None
        self.print_path = None
        self.threads = set()
        self.workers = []
        self.countdown = 0
        self.auto_capture_pending = False
        self.elapsed_start = monotonic()
        self.progress_phase = 1
        self.return_remaining = None
        self.renderer = HtmlRenderer(self)
        self.renderer.completed.connect(self._render_complete)
        self.renderer.failed.connect(self._render_failed)
        self.camera = CameraController(self)
        self.camera.frame_ready.connect(self._camera_frame)
        self.camera.status.connect(self._camera_status)
        self.countdown_timer = QTimer(self)
        self.countdown_timer.setInterval(1000)
        self.countdown_timer.timeout.connect(self._countdown_tick)
        self.clock = QTimer(self)
        self.clock.setInterval(1000)
        self.clock.timeout.connect(self._tick)
        self._build_ui()
        stylesheet = (RESOURCE_DIR / "styles.qss").read_text(encoding="utf-8")
        self.setStyleSheet(stylesheet.replace("__ASSETS__", (RESOURCE_DIR / "assets").as_posix()))
        self._select_style(0)
        self.clock.start()
        diagnostics = QAction(self)
        diagnostics.setShortcut("F8")
        diagnostics.triggered.connect(self._diagnostics)
        self.addAction(diagnostics)
        full_screen = QAction(self)
        full_screen.setShortcut("F11")
        full_screen.triggered.connect(self._toggle_fullscreen)
        self.addAction(full_screen)

    @property
    def selected_style(self) -> CityStyle:
        return STYLES[self.style_index]

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(14)
        header = QHBoxLayout()
        brand = QVBoxLayout()
        brand.setSpacing(5)
        brand.addWidget(label(APP_NAME, "brand"))
        brand.addWidget(label("CHENGDU / TIME PRINT", "eyebrow"))
        header.addLayout(brand)
        header.addStretch()
        self.date_label = label(local_time().strftime("%Y.%m.%d") + "\n成都", "date")
        self.date_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(self.date_label)
        layout.addLayout(header)
        steps = QHBoxLayout()
        steps.setSpacing(8)
        self.step_buttons = []
        for index, title in enumerate(("选择风格", "拍摄照片", "生成与打印")):
            step = QToolButton()
            step.setObjectName("step")
            step.setText(f"0{index + 1}   {title}")
            step.setCheckable(True)
            step.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            step.clicked.connect(lambda checked=False, number=index: self._jump(number))
            steps.addWidget(step, 1)
            self.step_buttons.append(step)
        layout.addLayout(steps)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        self._build_style_page()
        self._build_camera_page()
        self._build_result_page()
        footer = QHBoxLayout()
        footer.addWidget(label("RONGCHENG TIME PRINT", "footer"))
        footer.addStretch()
        self.return_label = label("", "footer")
        footer.addWidget(self.return_label)
        layout.addLayout(footer)

    def _page(self, title: str):
        page = QWidget()
        page.setObjectName("page")
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)
        if title:
            box.addWidget(label(title, "pageTitle"))
        self.pages.addWidget(page)
        return box

    def _build_style_page(self) -> None:
        box = self._page("")
        heading = QHBoxLayout()
        words = QVBoxLayout()
        words.setSpacing(5)
        self.style_title = label("", "styleTitle", True)
        self.style_detail = label("", "subtitle")
        words.addWidget(self.style_title)
        words.addWidget(self.style_detail)
        heading.addLayout(words, 1)
        counter = QVBoxLayout()
        self.style_counter = label("", "counter")
        self.style_counter.setAlignment(Qt.AlignRight)
        counter.addWidget(self.style_counter)
        counter.addWidget(label("效果示例", "eyebrow"))
        heading.addLayout(counter)
        box.addLayout(heading)
        self.cover_canvas = CoverCanvas()
        self.cover_canvas.stepped.connect(self._cycle_style)
        box.addWidget(self.cover_canvas, 1)
        strip = QHBoxLayout()
        strip.setSpacing(8)
        self.cover_buttons = []
        for index, style in enumerate(STYLES):
            thumb = CoverThumbnail(index, style.name, style.cover)
            thumb.clicked.connect(lambda checked=False, number=index: self._select_style(number))
            strip.addWidget(thumb, 1)
            self.cover_buttons.append(thumb)
        box.addLayout(strip)
        self.style_next = button("开始拍摄", True, "camera")
        self.style_next.setMinimumHeight(58)
        self.style_next.clicked.connect(self._confirm_style)
        box.addWidget(self.style_next)

    def _build_camera_page(self) -> None:
        box = self._page("")
        self.camera_heading = label("准备拍摄", "pageTitle")
        box.addWidget(self.camera_heading)
        self.camera_style = label("", "subtitle")
        box.addWidget(self.camera_style)
        self.camera_message = label("摄像头准备中", "subtitle", True)
        box.addWidget(self.camera_message)
        self.camera_preview = CameraPreview()
        box.addWidget(self.camera_preview, 1)
        actions = QHBoxLayout()
        self.camera_back = button("返回风格", icon="arrow-left")
        self.camera_back.clicked.connect(lambda: self._go(STYLE_STEP))
        self.camera_retry = icon_button("refresh-cw", "重新连接摄像头")
        self.camera_retry.clicked.connect(self._reconnect_camera)
        self.capture_button = button("开始倒计时", True, "camera")
        self.capture_button.clicked.connect(self._begin_countdown)
        self.photo_retake_button = button("重拍", icon="rotate-ccw")
        self.photo_retake_button.clicked.connect(self._open_camera)
        self.photo_use_button = button("使用照片生成", True, "arrow-right")
        self.photo_use_button.clicked.connect(self._confirm_photo)
        actions.addWidget(self.camera_back)
        actions.addWidget(self.camera_retry)
        actions.addWidget(self.photo_retake_button)
        actions.addWidget(self.capture_button, 1)
        actions.addWidget(self.photo_use_button, 1)
        box.addLayout(actions)

    def _build_result_page(self) -> None:
        box = self._page("城市纪念报")
        self.result_stack = QStackedWidget()
        box.addWidget(self.result_stack, 1)
        loading = QWidget()
        contents = QVBoxLayout(loading)
        contents.setContentsMargins(40, 20, 40, 20)
        contents.addStretch()
        self.creation_spinner = CreationSpinner()
        contents.addWidget(self.creation_spinner, 0, Qt.AlignHCenter)
        contents.addSpacing(24)
        self.creation_heading = label("正在创作城市影像", "progressTitle", True)
        self.creation_heading.setAlignment(Qt.AlignCenter)
        contents.addWidget(self.creation_heading)
        self.creation_style = label("", "subtitle")
        self.creation_style.setAlignment(Qt.AlignCenter)
        contents.addWidget(self.creation_style)
        self.elapsed_label = label("", "subtitle")
        self.elapsed_label.setAlignment(Qt.AlignCenter)
        contents.addWidget(self.elapsed_label)
        contents.addSpacing(28)
        self.stage_labels = []
        self.stage_states = []
        for title in ("照片与风格准备", "AI 画面创作", "报纸排版与高清输出"):
            row = QFrame()
            row.setObjectName("progressRow")
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 13, 0, 13)
            stage_title = label(title, "progressStage")
            stage_state = label("", "stageStatus")
            line.addWidget(stage_title, 1)
            line.addWidget(stage_state)
            contents.addWidget(row)
            self.stage_labels.append(stage_title)
            self.stage_states.append(stage_state)
        contents.addStretch()
        self.result_stack.addWidget(loading)
        ready = QWidget()
        ready_box = QVBoxLayout(ready)
        ready_box.setContentsMargins(0, 0, 0, 0)
        self.result_canvas = PosterCanvas()
        ready_box.addWidget(self.result_canvas, 1)
        self.result_message = label("A4 · 高清打印", "message", True)
        ready_box.addWidget(self.result_message)
        copies = QHBoxLayout()
        copies.addWidget(label("份数", "subtitle"))
        self.copy_less = icon_button("minus", "减少打印份数")
        copies.addWidget(self.copy_less)
        self.copy_spin = QSpinBox()
        self.copy_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.copy_spin.setAlignment(Qt.AlignCenter)
        self.copy_spin.setFixedWidth(64)
        self.copy_spin.setRange(1, 9)
        self.copy_spin.setValue(1)
        copies.addWidget(self.copy_spin)
        self.copy_more = icon_button("plus", "增加打印份数")
        copies.addWidget(self.copy_more)
        self.copy_less.clicked.connect(self.copy_spin.stepDown)
        self.copy_more.clicked.connect(self.copy_spin.stepUp)
        copies.addStretch()
        self.export_button = icon_button("file-down", "导出 PDF")
        self.export_button.clicked.connect(self._export_pdf)
        copies.addWidget(self.export_button)
        ready_box.addLayout(copies)
        self.result_stack.addWidget(ready)
        error_page = QWidget()
        error_box = QVBoxLayout(error_page)
        error_box.addStretch()
        self.error_label = label("", "errorText", True)
        self.error_label.setAlignment(Qt.AlignCenter)
        error_box.addWidget(self.error_label)
        self.retry_button = button("重试", True, "refresh-cw")
        self.retry_button.clicked.connect(self._retry_creation)
        error_box.addWidget(self.retry_button)
        error_box.addStretch()
        self.result_stack.addWidget(error_page)
        actions = QHBoxLayout()
        self.home_button = icon_button("house", "重新开始")
        self.home_button.clicked.connect(self._restart)
        self.retake_button = button("重拍", icon="rotate-ccw")
        self.retake_button.clicked.connect(self._open_camera)
        self.print_button = button("打印报纸", True, "printer")
        self.print_button.clicked.connect(self._print)
        actions.addWidget(self.home_button)
        actions.addWidget(self.retake_button)
        actions.addWidget(self.print_button, 1)
        box.addLayout(actions)
        self.copy_spin.valueChanged.connect(lambda: self._refresh_controls())

    def _select_style(self, index: int) -> None:
        if self.current_step != STYLE_STEP or self.busy or self.print_pending or not 0 <= index < len(STYLES):
            return
        if index != self.style_index:
            self._reset_result()
            self.max_step = STYLE_STEP
        self.style_index = index
        for number, thumb in enumerate(self.cover_buttons):
            thumb.setChecked(number == index)
        self.style_title.setText(self.selected_style.name)
        self.style_detail.setText(self.selected_style.location + "   /   " + self.selected_style.genre)
        self.style_counter.setText(f"{index + 1:02} / {len(STYLES):02}")
        self.cover_canvas.set_cover(self.selected_style.cover)
        self.camera_style.setText(self.selected_style.name + " / " + self.selected_style.genre)
        self.creation_style.setText(self.selected_style.name)
        self._refresh_controls()

    def _cycle_style(self, direction: int) -> None:
        if self.current_step == STYLE_STEP:
            self._select_style((self.style_index + direction) % len(STYLES))

    def _confirm_style(self) -> None:
        self._open_camera()

    def _jump(self, index: int) -> None:
        if index <= self.max_step and not self.busy and not self.print_pending:
            if index == CAMERA_STEP:
                self._open_camera()
            else:
                self._go(index)

    def _go(self, index: int) -> None:
        if index != CAMERA_STEP:
            self.countdown_timer.stop()
            self.countdown = 0
            self.auto_capture_pending = False
            self.camera.stop()
        self.current_step = index
        self.pages.setCurrentIndex(index)
        self._refresh_controls()

    def _open_camera(self) -> None:
        if self.busy or self.print_pending:
            return
        self._reset_result()
        self.max_step = CAMERA_STEP
        self.current_step = CAMERA_STEP
        self.pages.setCurrentIndex(CAMERA_STEP)
        self.camera_preview.set_frame(QImage())
        self.camera_preview.countdown = ""
        self.countdown = 0
        self.auto_capture_pending = True
        self.capture_button.setEnabled(False)
        self.camera.start()
        self._refresh_controls()

    def _reconnect_camera(self) -> None:
        self.countdown_timer.stop()
        self.countdown = 0
        self.camera.stop()
        self._open_camera()

    def _camera_status(self, text: str) -> None:
        if self.current_step == CAMERA_STEP and self.current_photo.isNull():
            self.camera_message.setText(text)

    def _camera_frame(self, image: QImage) -> None:
        if self.current_step != CAMERA_STEP or not self.current_photo.isNull():
            return
        self.camera_preview.set_frame(image)
        self.capture_button.setEnabled(self.countdown == 0)
        if self.auto_capture_pending:
            self.auto_capture_pending = False
            session = self.session_id
            QTimer.singleShot(1200, lambda: self._begin_countdown() if self.current_step == CAMERA_STEP and self.session_id == session else None)

    def _begin_countdown(self) -> None:
        if (self.current_step != CAMERA_STEP or self.countdown > 0
                or not self.current_photo.isNull() or self.camera.last_frame.isNull()
                or self.busy or self.print_pending):
            return
        self.auto_capture_pending = False
        self.countdown = CAPTURE_SECONDS
        self.camera_preview.countdown = str(self.countdown)
        self.camera_preview.update()
        self.camera_message.setText("看向镜头，保持自然")
        self.capture_button.setEnabled(False)
        self.camera_retry.setEnabled(False)
        self.countdown_timer.start()

    def _countdown_tick(self) -> None:
        if (self.current_step != CAMERA_STEP or self.countdown <= 0
                or not self.current_photo.isNull() or self.busy or self.print_pending):
            return
        self.countdown -= 1
        if self.countdown > 0:
            self.camera_preview.countdown = str(self.countdown)
            self.camera_preview.update()
            return
        self.countdown_timer.stop()
        self.auto_capture_pending = False
        self.camera_preview.countdown = ""
        image = self.camera_preview.capture_frame(self.camera.last_frame)
        if image.isNull():
            self.camera_message.setText("没有可用画面，请重新连接摄像头。")
            self._refresh_controls()
            return
        self.current_photo = image
        self.photo_confirmed = False
        self.timestamp = local_time()
        self.camera.stop()
        self.camera_preview.set_photo(image)
        self.camera_message.setText("照片已拍摄")
        self._refresh_controls()

    def _confirm_photo(self) -> None:
        if (self.current_step != CAMERA_STEP or self.current_photo.isNull()
                or self.photo_confirmed or self.busy or self.print_pending):
            return
        self.photo_confirmed = True
        self.max_step = RESULT_STEP
        self._go(RESULT_STEP)
        self._generate()

    def _generate(self) -> None:
        if self.busy or self.print_pending or self.current_photo.isNull() or not self.photo_confirmed:
            return
        key = load_api_key()
        if not key:
            self._error("尚未配置 AI 服务，请设置 HUOSHAN_API_KEY 后重试。")
            return
        self.busy = True
        self.progress_phase = 1
        self.elapsed_start = monotonic()
        self.elapsed_label.setText("已等待 0 秒")
        self.result_stack.setCurrentIndex(0)
        self._refresh_controls()
        session = DATA_DIR / "sessions" / self.session_id
        session.mkdir(parents=True, exist_ok=True)
        worker = GenerationWorker(self.current_photo, self.selected_style, key, session / "visual.png")
        self._start_worker(worker, self._generation_finished)

    def _start_worker(self, worker, callback) -> None:
        thread = QThread(self)
        self.threads.add(thread)
        self.workers.append(worker)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(callback)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda current=thread, task=worker: self._thread_finished(current, task))
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _thread_finished(self, thread, worker) -> None:
        self.threads.discard(thread)
        if worker in self.workers:
            self.workers.remove(worker)
        if self.closing and not self.threads:
            QTimer.singleShot(0, self._finish_close)

    def _generation_finished(self, ok: bool, result: str) -> None:
        if not ok:
            self.busy = False
            self._error(result)
            return
        self.generated_path = Path(result)
        self.timestamp = local_time()
        self._render_final()

    def _render_final(self) -> None:
        self.busy = True
        self.progress_phase = 2
        self.result_stack.setCurrentIndex(0)
        self._refresh_controls()
        self.renderer.submit(RenderJob(f"final:{self.session_id}", self.selected_style, self.generated_path, self.timestamp, dpi=300), priority=True)

    def _render_complete(self, key: str, pdf: str, png: str) -> None:
        if key != f"final:{self.session_id}":
            return
        self.pdf_path = Path(pdf)
        self.print_path = Path(png)
        self.busy = False
        self.result_canvas.set_poster(png)
        self.result_stack.setCurrentIndex(1)
        self.result_message.setText(self.selected_style.name + " · A4 · 高清打印")
        self.return_remaining = RETURN_SECONDS
        self._refresh_controls()
        if self.closing:
            self._finish_close()

    def _render_failed(self, key: str, message: str) -> None:
        if key == f"final:{self.session_id}":
            self.busy = False
            self._error(message)

    def _error(self, message: str) -> None:
        self.error_label.setText(message)
        self.result_stack.setCurrentIndex(2)
        self.return_remaining = None
        self._refresh_controls()
        if self.closing and not self.threads:
            self._finish_close()

    def _finish_close(self) -> None:
        host = getattr(self, "host_window", self)
        host.close()

    def _retry_creation(self) -> None:
        if self.busy:
            return
        if self.generated_path is not None and self.generated_path.is_file():
            self._render_final()
        else:
            self._generate()

    def _refresh_controls(self) -> None:
        locked = self.busy or self.print_pending
        for index, step in enumerate(self.step_buttons):
            step.setChecked(index == self.current_step)
            step.setEnabled(index <= self.max_step and not locked)
        for thumb in self.cover_buttons:
            thumb.setEnabled(not locked)
        self.cover_canvas.setEnabled(not locked)
        self.style_next.setEnabled(not locked)
        self.home_button.setEnabled(not locked)
        self.retake_button.setEnabled(not locked)
        self.retry_button.setEnabled(not locked)
        reviewing = not self.current_photo.isNull() and not self.photo_confirmed
        self.camera_heading.setText("确认照片" if reviewing else "准备拍摄")
        self.camera_back.setEnabled(not locked)
        self.camera_retry.setVisible(not reviewing)
        self.camera_retry.setEnabled(self.countdown == 0 and not locked and not reviewing)
        self.capture_button.setVisible(not reviewing)
        self.capture_button.setEnabled(self.current_step == CAMERA_STEP and not reviewing
                                       and self.countdown == 0 and not locked and not self.camera.last_frame.isNull())
        self.photo_retake_button.setVisible(reviewing)
        self.photo_use_button.setVisible(reviewing)
        self.photo_retake_button.setEnabled(reviewing and not locked)
        self.photo_use_button.setEnabled(reviewing and not locked)
        can_print = self.print_path is not None and not locked and not self.print_locked
        self.print_button.setEnabled(can_print)
        self.print_button.setText("请检查打印队列" if self.print_state == "unknown" else "已提交打印" if self.print_locked else "正在提交" if self.print_pending else "打印报纸")
        self.copy_spin.setEnabled(can_print)
        self.copy_less.setEnabled(can_print and self.copy_spin.value() > 1)
        self.copy_more.setEnabled(can_print and self.copy_spin.value() < 9)
        self.export_button.setEnabled(self.pdf_path is not None and not locked)
        self.creation_heading.setText("正在排版城市纪念报" if self.progress_phase == 2 else "正在创作城市影像")
        for index, item in enumerate(self.stage_labels):
            text = ("照片与风格准备", "AI 画面创作", "报纸排版与高清输出")[index]
            item.setText(f"{index + 1:02}    {text}")
            item.setProperty("active", index == self.progress_phase)
            item.style().unpolish(item)
            item.style().polish(item)
            status = self.stage_states[index]
            status.setText("已完成" if index < self.progress_phase else "进行中" if index == self.progress_phase else "待开始")
            status.setProperty("active", index == self.progress_phase)
            status.style().unpolish(status)
            status.style().polish(status)

    def _print(self) -> None:
        if self.print_path is None or self.busy or self.print_pending or self.print_locked:
            return
        try:
            printer = physical_printer()
        except RuntimeError as error:
            self.result_message.setText(str(error))
            return
        self.print_pending = True
        self.return_remaining = None
        self._refresh_controls()
        self.result_message.setText("正在提交打印，请稍候。")
        self._start_worker(PrintWorker(self.print_path, printer, self.copy_spin.value()), self._printed)

    def _printed(self, state: str, message: str) -> None:
        self.print_pending = False
        self.print_locked = state in ("submitted", "unknown")
        self.print_state = state
        self.result_message.setText(message)
        self.return_remaining = RETURN_SECONDS
        self._refresh_controls()

    def _export_pdf(self) -> None:
        if self.pdf_path is None:
            return
        self.return_remaining = None
        target, _ = QFileDialog.getSaveFileName(self, "保存报纸", str(Path.home() / "Desktop" / f"蓉城时光印_{self.timestamp:%Y%m%d_%H%M%S}.pdf"), "PDF (*.pdf)")
        if target:
            try:
                shutil.copy2(self.pdf_path, target)
                self.result_message.setText("电子报纸已保存。")
            except OSError as error:
                self.result_message.setText(f"保存失败：{error}")
        self.return_remaining = RETURN_SECONDS

    def _reset_result(self) -> None:
        self.countdown_timer.stop()
        self.countdown = 0
        self.auto_capture_pending = False
        self.session_id = uuid4().hex
        self.current_photo = QImage()
        self.photo_confirmed = False
        self.camera_preview.countdown = ""
        self.camera_preview.set_frame(QImage())
        self.camera.last_frame = QImage()
        self.generated_path = None
        self.pdf_path = None
        self.print_path = None
        self.print_pending = False
        self.print_locked = False
        self.print_state = ""
        self.return_remaining = None
        self.copy_spin.setValue(1)
        self.result_canvas.set_poster(None)

    def _restart(self) -> None:
        if self.busy or self.print_pending:
            return
        self._reset_result()
        self.max_step = 0
        self._go(0)

    def _tick(self) -> None:
        self.date_label.setText(local_time().strftime("%Y.%m.%d") + "\n成都")
        if self.busy:
            self.elapsed_label.setText(f"已等待 {int(monotonic() - self.elapsed_start)} 秒")
        if self.current_step == RESULT_STEP and not self.busy and not self.print_pending and self.return_remaining is not None:
            self.return_remaining -= 1
            self.return_label.setText(f"{max(0, self.return_remaining)} 秒后返回首页")
            if self.return_remaining <= 0:
                self._restart()
        else:
            self.return_label.setText("")

    def _diagnostics(self) -> None:
        QMessageBox.information(self, "设备状态", f"AI 配置：{'已配置' if load_api_key() else '未配置'}\n模型：{SEEDREAM_MODEL}\n摄像头：{self.camera.format_text}\n当前风格：{self.selected_style.name}\n打印状态：{self.print_state or '未提交'}\n运行数据：{DATA_DIR}")

    def _toggle_fullscreen(self) -> None:
        outer = getattr(self, "host_window", self)
        outer.showNormal() if outer.isFullScreen() else outer.showFullScreen()

    def closeEvent(self, event) -> None:
        if self.threads or self.busy or self.print_pending:
            self.closing = True
            event.ignore()
            return
        self.camera.stop()
        self.renderer.shutdown()
        event.accept()


class RotatedWindow(QMainWindow):
    def __init__(self, inner: BoothWindow) -> None:
        super().__init__()
        self.inner = inner
        inner.host_window = self
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(inner.windowIcon())
        self.scene = QGraphicsScene(self)
        self.proxy = self.scene.addWidget(inner)
        self.proxy.setRotation(-90)
        self.view = QGraphicsView(self.scene)
        self.view.setFrameShape(QFrame.NoFrame)
        self.view.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.viewport().installEventFilter(self)
        self.setCentralWidget(self.view)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._fit_view)

    def eventFilter(self, watched, event) -> bool:
        if watched is self.view.viewport() and event.type() == QEvent.Resize:
            QTimer.singleShot(0, self._fit_view)
        return super().eventFilter(watched, event)

    def _fit_view(self) -> None:
        viewport = self.view.viewport().size()
        self.inner.resize(viewport.height(), viewport.width())
        rect = self.proxy.sceneBoundingRect()
        self.scene.setSceneRect(rect)
        self.view.fitInView(rect, Qt.KeepAspectRatio)

    def closeEvent(self, event) -> None:
        if self.inner.threads or self.inner.busy or self.inner.print_pending:
            self.inner.closing = True
            event.ignore()
        else:
            self.inner.camera.stop()
            self.inner.renderer.shutdown()
            event.accept()
