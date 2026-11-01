from __future__ import annotations

import base64
import html
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QMarginsF, QSize, QTimer, QUrl, Signal
from PySide6.QtGui import QPageLayout, QPageSize
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings, QWebEngineUrlRequestInterceptor

from catalog import CityStyle
from paths import DATA_DIR, RESOURCE_DIR, log


BEIJING_TIME = timezone(timedelta(hours=8))


def local_time() -> datetime:
    return datetime.now(BEIJING_TIME)


class LiveTimeFields(HTMLParser):
    def __init__(self, values: dict[str, str], timestamp: datetime) -> None:
        super().__init__(convert_charrefs=False)
        self.values = values
        self.timestamp = timestamp
        self.parts = []
        self.field = None

    def handle_starttag(self, tag, attrs) -> None:
        if self.field is not None:
            raise ValueError("Live time fields must contain text only.")
        self.parts.append(self.get_starttag_text())
        attributes = dict(attrs)
        name = attributes.get("data-live-field")
        if name is not None:
            if tag != "time":
                raise ValueError(f"Unsupported live time field: {name}")
            if name == "story-time":
                minutes = int(attributes["data-minutes-before"])
                if not 0 <= minutes <= 1440:
                    raise ValueError("Story time offset must be between 0 and 1440 minutes.")
                moment = self.timestamp - timedelta(minutes=minutes)
                time_format = "%H:%M" if moment.date() == self.timestamp.date() else "%m.%d %H:%M"
                value = moment.strftime(time_format)
            elif name in self.values:
                value = self.values[name]
            else:
                raise ValueError(f"Unsupported live time field: {name}")
            self.parts.append(html.escape(value))
            self.field = name

    def handle_endtag(self, tag) -> None:
        if self.field is not None and tag != "time":
            raise ValueError("Unclosed live time field.")
        self.parts.append(f"</{tag}>")
        self.field = None

    def handle_startendtag(self, tag, attrs) -> None:
        if self.field is not None:
            raise ValueError("Live time fields must contain text only.")
        self.parts.append(self.get_starttag_text())

    def handle_data(self, data) -> None:
        if self.field is None:
            self.parts.append(data)

    def handle_entityref(self, name) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name) -> None:
        self.handle_data(f"&#{name};")

    def handle_comment(self, data) -> None:
        self.handle_data(f"<!--{data}-->")

    def handle_decl(self, decl) -> None:
        self.handle_data(f"<!{decl}>")


def build_html(style: CityStyle, image: Path | None, timestamp: datetime) -> str:
    photo = image or RESOURCE_DIR / "assets" / "placeholder.svg"
    if not photo.is_file():
        raise RuntimeError("报纸照片文件不存在。")
    mime = "image/svg+xml" if photo.suffix.lower() == ".svg" else "image/png" if photo.suffix.lower() == ".png" else "image/jpeg"
    uri = f"data:{mime};base64," + base64.b64encode(photo.read_bytes()).decode("ascii")
    markup = style.template.read_text(encoding="utf-8")
    stamp = timestamp.replace(tzinfo=BEIJING_TIME) if timestamp.tzinfo is None else timestamp.astimezone(BEIJING_TIME)
    date = stamp.strftime("%Y.%m.%d")
    weekday = "星期" + "一二三四五六日"[stamp.weekday()]
    clock = stamp.strftime("%H:%M")
    # Only explicitly marked metadata changes; article dates remain untouched.
    fields = LiveTimeFields({"date-time": f"{date} {weekday} {clock}", "date-weekday": f"{date} {weekday}", "time": clock}, stamp)
    fields.feed(markup.replace("__PHOTO_URI__", html.escape(uri, quote=True)))
    fields.close()
    if fields.field is not None:
        raise ValueError("Unclosed live time field.")
    return "".join(fields.parts)


@dataclass
class RenderJob:
    key: str
    style: CityStyle
    image: Path | None
    timestamp: datetime
    dpi: int = 300


class LocalContentOnly(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info) -> None:
        if info.requestUrl().scheme() not in ("file", "data", "qrc", "about", "blob"):
            info.block(True)


class HtmlRenderer(QObject):
    completed = Signal(str, str, str)
    failed = Signal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.profile = QWebEngineProfile(self)
        self.interceptor = LocalContentOnly(self.profile)
        self.profile.setUrlRequestInterceptor(self.interceptor)
        self.jobs = deque()
        self.current = None
        self.page = None
        self.folder = None
        self.timeout = QTimer(self)
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(lambda: self._fail("报纸渲染超时，请重试。"))
        self.poll = QTimer(self)
        self.poll.setInterval(100)
        self.poll.timeout.connect(self._check_ready)
        self.printing = False
        self.closing = False

    @property
    def busy(self) -> bool:
        return self.current is not None

    def submit(self, job: RenderJob, priority: bool = False) -> None:
        if priority:
            self.jobs.appendleft(job)
        else:
            self.jobs.append(job)
        if not self.busy:
            QTimer.singleShot(0, self._next)

    def _next(self) -> None:
        if self.busy or not self.jobs or self.closing:
            return
        self.current = self.jobs.popleft()
        token = self.current.key
        self.folder = DATA_DIR / "renders" / uuid4().hex
        try:
            self.folder.mkdir(parents=True, exist_ok=False)
            document = self.folder / "poster.html"
            document.write_text(build_html(self.current.style, self.current.image, self.current.timestamp), encoding="utf-8")
            self.page = QWebEnginePage(self.profile, self)
            self.page.settings().setAttribute(QWebEngineSettings.PrintElementBackgrounds, True)
            self.page.loadFinished.connect(lambda ok, key=token: self._loaded(key, ok))
            self.page.pdfPrintingFinished.connect(lambda path, ok, key=token: self._pdf_ready(key, path, ok))
            self.printing = False
            self.timeout.start(45000)
            self.page.load(QUrl.fromLocalFile(str(document)))
        except Exception as error:
            self._fail(str(error))

    def _loaded(self, key: str, ok: bool) -> None:
        if not self.current or self.current.key != key:
            return
        if not ok:
            self._fail("HTML 模板加载失败。")
            return
        self.poll.start()

    def _check_ready(self) -> None:
        if self.page is None or self.printing:
            return
        token = self.current.key
        self.page.runJavaScript("document.fonts.status === 'loaded' && Array.from(document.images).every(img => img.complete && img.naturalWidth > 0)", lambda ready, key=token: self._begin_pdf(key, ready))

    def _begin_pdf(self, key: str, ready: bool) -> None:
        if not self.current or self.current.key != key or not ready or self.printing:
            return
        self.printing = True
        self.poll.stop()
        layout = QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait, QMarginsF(0, 0, 0, 0))
        self.page.printToPdf(str(self.folder / "poster.pdf"), layout)

    def _pdf_ready(self, key: str, path: str, ok: bool) -> None:
        if not self.current or self.current.key != key:
            return
        if not ok:
            self._fail("PDF 输出失败。")
            return
        document = QPdfDocument(self)
        try:
            error = document.load(path)
            if error != QPdfDocument.Error.None_ or document.pageCount() != 1:
                raise RuntimeError(f"报纸需为单页，当前输出 {document.pageCount()} 页。")
            dpi = self.current.dpi
            size = QSize(round(210 / 25.4 * dpi), round(297 / 25.4 * dpi))
            image = document.render(0, size)
            png = self.folder / "poster.png"
            if image.isNull() or not image.save(str(png), "PNG"):
                raise RuntimeError("打印图输出失败。")
        except Exception as error:
            self._fail(str(error))
            return
        finally:
            document.close()
            document.deleteLater()
        self._cleanup()
        self.completed.emit(key, path, str(png))
        QTimer.singleShot(0, self._next)

    def _fail(self, message: str) -> None:
        if not self.current:
            return
        key = self.current.key
        log(f"render failed: {message}")
        self._cleanup()
        self.failed.emit(key, message)
        QTimer.singleShot(0, self._next)

    def _cleanup(self) -> None:
        self.poll.stop()
        self.timeout.stop()
        if self.page is not None:
            self.page.deleteLater()
            self.page = None
        self.current = None
        self.printing = False

    def shutdown(self) -> None:
        self.closing = True
        self.jobs.clear()
        self._cleanup()
