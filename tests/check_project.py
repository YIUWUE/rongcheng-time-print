from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
ARTIFACTS = Path(os.environ.get("RONGCHENG_TEST_OUTPUT", tempfile.mkdtemp(prefix="rongcheng-check-")))
ARTIFACTS.mkdir(parents=True, exist_ok=True)
os.environ["RONGCHENG_DATA_DIR"] = str(ARTIFACTS / "runtime")
os.environ.setdefault("QT_QPA_PLATFORM", "windows" if os.name == "nt" else "offscreen")
sys.path.insert(0, str(PROJECT))

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRectF, QUrl, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
APP = QApplication.instance() or QApplication([])
APP.setStyle("Fusion")
if APP.platformName() == "offscreen" and os.name == "nt":
    for filename in ("msyh.ttc", "msyhbd.ttc", "arial.ttf", "simsun.ttc"):
        QFontDatabase.addApplicationFont(str(Path(os.environ["SystemRoot"]) / "Fonts" / filename))
APP.setFont(QFont("Microsoft YaHei", 11))
APP.setQuitOnLastWindowClosed(False)

import ui
from camera import CameraController, CameraPreview
from catalog import STYLES, STYLE_BY_ID, build_prompt
from rendering import RenderJob, build_html
from services import PrintWorker, SeedreamClient, image_data_uri
from PySide6.QtWebEngineCore import QWebEnginePage

TEXT_GEOMETRY = """(() => {
  const root=document.getElementById('newspaper-root'), bounds=root.getBoundingClientRect();
  const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT), texts=[];
  while(walker.nextNode()) {
    const node=walker.currentNode, text=node.textContent.trim();
    if(!text || node.parentElement.closest('script,style')) continue;
    const range=document.createRange(); range.selectNodeContents(node);
    for(const r of range.getClientRects()) if(r.width>1 && r.height>1)
      texts.push({text:text.slice(0,40),x:r.x,y:r.y,w:r.width,h:r.height});
  }
  const overlaps=[];
  for(let i=0;i<texts.length;i++) for(let j=i+1;j<texts.length;j++) {
    const a=texts[i],b=texts[j];
    const w=Math.min(a.x+a.w,b.x+b.w)-Math.max(a.x,b.x);
    const h=Math.min(a.y+a.h,b.y+b.h)-Math.max(a.y,b.y);
    if(w>2 && h>Math.min(a.h,b.h)*0.6 && w*h>Math.min(a.w*a.h,b.w*b.h)*0.15)
      overlaps.push([a.text,b.text]);
  }
  const clipped=texts.filter(r=>r.x<bounds.x-1 || r.y<bounds.y-1 || r.x+r.w>bounds.right+1 || r.y+r.h>bounds.bottom+1);
  const hiddenByContainer=[];
  const inside=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
  while(inside.nextNode()) {
    const node=inside.currentNode, text=node.textContent.trim();
    if(!text || node.parentElement.closest('script,style')) continue;
    const range=document.createRange(); range.selectNodeContents(node);
    for(let parent=node.parentElement;parent && parent!==root;parent=parent.parentElement) {
      const css=getComputedStyle(parent), box=parent.getBoundingClientRect();
      const clipX=['hidden','clip'].includes(css.overflowX),clipY=['hidden','clip'].includes(css.overflowY);
      if(!clipX && !clipY) continue;
      for(const r of range.getClientRects()) if(r.width>1 && r.height>1 &&
        ((clipX && (r.x<box.x-1 || r.right>box.right+1)) ||
         (clipY && (r.y<box.y-1 || r.bottom>box.bottom+1))))
          hiddenByContainer.push({text:text.slice(0,40),container:parent.className});
    }
  }
  return JSON.stringify({overlaps,clipped,hiddenByContainer,metadata:root.querySelector('.info')?.innerText || ''});
})()"""


APPROVED_STORY_EDITS = {
    "fashion": (
        ("15:42 · 晴 15℃ · 西南偏西硬光", "15:42"),
        ("你在成都的第 002 天", ""),
        ("地点：春熙路 IFS 七层平台", ""),
        ("每个人的成都，都从某一分钟开始", "春熙路漫游 · 一段城市留念"),
        ("拐进春熙路", "走进春熙路"),
        ("人比想象中多", "从街角开始漫游"),
        ("抬头撞见熊猫", "抬头遇见熊猫"),
        ("十五米，趴在墙上", "与城市交换目光"),
        ("15:35举起手机手搭在额前遮光", "15:35停在玻璃幕墙前光影映进这一天"),
        ("按下快门", "举起镜头"),
        ("报纸开始吐纸", "留住此刻的自己"),
    ),
    "ink": (
        ("09:36 · 小雨 16℃ · 天光漫射", "09:36"),
        ("你在成都的第 002 天", ""),
        ("地点：杜甫草堂 · 花径", ""),
        ("每个人的成都，都从某一分钟开始", "草堂漫步 · 一段诗意留念"),
        ("走进南门", "走进草堂"),
        ("游客还没来", "把喧嚣留在门外"),
        ("09:18穿过红墙花径墙外是竹，墙里是影", "09:18穿过红墙花径沿竹影缓步而行"),
        ("在茅屋檐下", "停在茅屋檐下"),
        ("听雨一滴一滴", "翻开一卷旧诗"),
        ("举起手机", "留影竹径"),
        ("手搭在额前", "收藏片刻的从容"),
    ),
}


def wait_for(condition, timeout=60000):
    deadline = time.monotonic() + timeout / 1000
    while not condition() and time.monotonic() < deadline:
        QTest.qWait(25)
    if not condition():
        raise AssertionError("Timed out waiting for Qt task")


def sample_image(width=1920, height=1080):
    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(QColor("#315a47"))
    painter = QPainter(image)
    painter.fillRect(0, 0, width // 2, height, QColor("#b95c4b"))
    painter.fillRect(width // 2, 0, width - width // 2, height, QColor("#47758a"))
    painter.end()
    return image


class TemplateContent(HTMLParser):
    def __init__(self, markup, ignore_live_fields=False):
        super().__init__(convert_charrefs=True)
        self.in_body = False
        self.in_title = False
        self.ignored = 0
        self.title = ""
        self.text = []
        self.static_images = []
        self.ignore_live_fields = ignore_live_fields
        self.live_fields = {}
        self.story_times = []
        self.field = None
        self.feed(markup)

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "time" and "data-live-field" in attributes:
            self.field = attributes["data-live-field"]
            self.live_fields[self.field] = ""
        if tag == "body":
            self.in_body = True
        elif tag == "title":
            self.in_title = True
        elif tag in ("style", "script"):
            self.ignored += 1
        elif tag == "img" and attributes.get("id") != "ai-photo":
            self.static_images.append(hashlib.sha256(attributes.get("src", "").encode()).hexdigest())

    def handle_startendtag(self, tag, attributes):
        self.handle_starttag(tag, attributes)
        if tag in ("style", "script"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "time":
            if self.field == "story-time":
                self.story_times.append(self.live_fields[self.field])
            self.field = None
        if tag == "body":
            self.in_body = False
        elif tag == "title":
            self.in_title = False
        elif tag in ("style", "script"):
            self.ignored -= 1

    def handle_data(self, value):
        if self.ignored:
            return
        value = " ".join(value.split())
        if self.field:
            self.live_fields[self.field] += value
            if self.ignore_live_fields:
                return
        if self.in_title:
            self.title += value
        elif self.in_body and value:
            self.text.append(value)


class ProjectChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.window = ui.BoothWindow()
        cls.window.resize(760, 1280)
        cls.window.show()
        QTest.qWait(50)

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        cls.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def setUp(self):
        self.window.closing = False
        self.window._restart()

    def capture_photo(self, image=None):
        window = self.window
        image = sample_image() if image is None else image
        with patch.object(window.camera, "start"):
            window._confirm_style()
        window.camera._frame(SimpleNamespace(toImage=lambda: image))
        window._begin_countdown()
        window.countdown = 1
        window._countdown_tick()

    def test_catalog_and_template_binding(self):
        self.assertEqual(len(STYLES), 7)
        self.assertEqual(len(STYLE_BY_ID), 7)
        self.assertEqual([style.id for style in STYLES], ["qingcheng", "cyber", "panda", "ear_cleaning", "chef", "fashion", "ink"])
        for index, style in enumerate(STYLES):
            self.assertTrue(style.template.is_file())
            self.assertTrue(style.cover.is_file())
            self.assertFalse(QImage(str(style.cover)).isNull())
            self.window._select_style(index)
            self.assertEqual(self.window.selected_style.id, style.id)
            self.assertEqual(self.window.cover_canvas.image_path, style.cover)
            self.assertTrue(self.window.cover_buttons[index].isChecked())
            self.assertEqual(sum(thumb.isChecked() for thumb in self.window.cover_buttons), 1)
        qingcheng = STYLE_BY_ID["qingcheng"]
        self.assertIn("青城天下幽", build_prompt(qingcheng))
        self.assertIn("始终与手掌连接", build_prompt(qingcheng))
        self.assertIn("不要成年化", build_prompt(qingcheng))

    def test_original_prompts_are_unchanged(self):
        expected = {
            "panda": "d7db5ef4073e72d4669771f8d6b1314a32122c768c4417f0e49920f4af7d656b",
            "ear_cleaning": "dfe1e475b4718a5dc2ad9e2ad5f7bcc0b6bdff212e2e26d0361ae13e83550b39",
            "chef": "27af4b13400cf9744938b8fa79dba9dfb8eea4c581bf0e1d1231bf87985af9a7",
            "cyber": "5376a2db6cdd9216295b6dd138ee89e7db769db9d734f8706c1a4f1bccced54a",
            "fashion": "0345193e3122774710f023eabcc80badee7ebd2cf75568e673e14e561dd24283",
            "ink": "0cb0166f78ef777a0dfc3adcdc26ddf65d7e31016f8506750c6eb7d5952862b6",
            "qingcheng": "e94024aab0fce0ab097567d117c56cc257cb5470c8ecfe75a8daf1501ac0117d",
        }
        for style in STYLES:
            self.assertEqual(hashlib.sha256(style.prompt.encode()).hexdigest(), expected[style.id])

    def test_carousel_buttons_swipe_and_keyboard(self):
        window = self.window
        window.resize(760, 1280)
        window._select_style(0)
        QTest.mouseClick(window.cover_canvas.next_button, Qt.LeftButton)
        self.assertEqual(window.style_index, 1)
        QTest.mouseClick(window.cover_canvas.previous_button, Qt.LeftButton)
        self.assertEqual(window.style_index, 0)
        QTest.mouseClick(window.cover_canvas.previous_button, Qt.LeftButton)
        self.assertEqual(window.style_index, 6)
        QTest.mouseClick(window.cover_buttons[2], Qt.LeftButton)
        self.assertEqual(window.style_index, 2)
        window.cover_canvas.setFocus()
        QTest.keyClick(window.cover_canvas, Qt.Key_Right)
        self.assertEqual(window.style_index, 3)
        center = window.cover_canvas.rect().center()
        QTest.mousePress(window.cover_canvas, Qt.LeftButton, pos=center + QPoint(100, 0))
        QTest.mouseRelease(window.cover_canvas, Qt.LeftButton, pos=center - QPoint(100, 0))
        self.assertEqual(window.style_index, 4)
        QTest.mousePress(window.cover_canvas, Qt.LeftButton, pos=center)
        QTest.mouseRelease(window.cover_canvas, Qt.LeftButton, pos=center + QPoint(0, 100))
        self.assertEqual(window.style_index, 4)
        window.busy = True
        window._refresh_controls()
        window._cycle_style(1)
        self.assertEqual(window.style_index, 4)
        self.assertFalse(window.cover_canvas.isEnabled())
        window.busy = False
        window._refresh_controls()

    def test_three_step_flow_has_no_template_selector(self):
        window = self.window
        self.assertEqual(window.pages.count(), 3)
        self.assertEqual(len(window.step_buttons), 3)
        self.assertFalse(hasattr(window, "template_combo"))
        with patch.object(window.camera, "start") as start:
            for index in range(7):
                window._restart()
                window._select_style(index)
                QTest.mouseClick(window.style_next, Qt.LeftButton)
                self.assertEqual(window.current_step, ui.CAMERA_STEP)
                self.assertEqual(window.pages.currentIndex(), ui.CAMERA_STEP)
                self.assertEqual(window.selected_style.id, STYLES[index].id)
                QTest.mouseClick(window.camera_back, Qt.LeftButton)
                self.assertEqual(window.current_step, ui.STYLE_STEP)
            self.assertEqual(start.call_count, 7)
        self.assertFalse(window.renderer.busy)
        self.assertEqual(len(window.renderer.jobs), 0)

    def test_html_placeholders_and_clean_assets(self):
        stamp = datetime(2030, 3, 4, 5, 6, tzinfo=timezone(timedelta(hours=8)))
        forbidden = ("101 LAB", "101实验室", "school-logo", "campus-logo")
        for style in STYLES:
            markup = build_html(style, None, stamp)
            self.assertEqual(markup.count('id="ai-photo"'), 1, style.id)
            self.assertIn("2030.03.04", markup, style.id)
            self.assertIn("星期一", markup, style.id)
            self.assertIn("05:06", markup, style.id)
            self.assertNotIn("__PHOTO_URI__", markup)
            self.assertNotIn("__DATE__", markup)
            self.assertNotIn("__TIME__", markup)
            self.assertNotIn("__WEEKDAY__", markup)
            self.assertNotIn("<script", markup.lower())
            self.assertNotIn("http", markup.lower())
            for token in forbidden:
                self.assertNotIn(token, markup, (style.id, token))

    def test_template_text_matches_uploaded_originals(self):
        fixture = PROJECT / "tests" / "fixtures" / "original_template_content.json"
        expected = json.loads(fixture.read_text(encoding="utf-8"))
        for style in STYLES:
            with self.subTest(style=style.id):
                content = TemplateContent(style.template.read_text(encoding="utf-8"))
                self.assertEqual(content.title, expected[style.id]["title"])
                normalize = lambda text: "".join(text.split())
                original_text = normalize("".join(expected[style.id]["text"]))
                for before, after in APPROVED_STORY_EDITS.get(style.id, ()):
                    self.assertEqual(original_text.count(normalize(before)), 1, before)
                    original_text = original_text.replace(normalize(before), normalize(after), 1)
                self.assertEqual(normalize("".join(content.text)), original_text)
                self.assertEqual(content.static_images, expected[style.id]["static_images"])

    def test_render_updates_only_approved_fields(self):
        first = datetime(2030, 3, 4, 5, 6, tzinfo=timezone(timedelta(hours=8)))
        second = first + timedelta(days=10, hours=3)
        photo = ARTIFACTS / "photo-replacement.png"
        sample_image(1200, 1600).save(str(photo))
        for style in STYLES:
            with self.subTest(style=style.id):
                original = TemplateContent(style.template.read_text(encoding="utf-8"), ignore_live_fields=True)
                markup = build_html(style, photo, first)
                later = build_html(style, photo, second)
                self.assertNotEqual(markup, later)
                self.assertEqual(TemplateContent(markup, ignore_live_fields=True).text, original.text)
                self.assertEqual(TemplateContent(later, ignore_live_fields=True).text, original.text)
                self.assertIn("2030.03.14", later)
                self.assertIn("08:06", later)
                self.assertIn("data:image/png;base64,", markup)

    def test_print_time_uses_beijing_date_across_midnight(self):
        stamp = datetime(2030, 3, 3, 21, 6, tzinfo=timezone.utc)
        for style in STYLES:
            with self.subTest(style=style.id):
                markup = build_html(style, None, stamp)
                fields = TemplateContent(markup).live_fields
                self.assertTrue(fields)
                text = " ".join(fields.values())
                self.assertIn("2030.03.04", text)
                self.assertIn("星期一", text)
                self.assertIn("05:06", text)

    def test_generation_completion_records_actual_time(self):
        completed = datetime(2030, 3, 4, 0, 1, tzinfo=timezone(timedelta(hours=8)))
        self.window.timestamp = completed - timedelta(minutes=2)
        with patch("ui.local_time", return_value=completed), patch.object(self.window, "_render_final") as render:
            self.window._generation_finished(True, str(ARTIFACTS / "completed.png"))
        self.assertEqual(self.window.timestamp, completed)
        render.assert_called_once()

    def test_story_timelines_end_at_real_time(self):
        stamp = datetime(2030, 3, 4, 0, 10, 45, tzinfo=timezone(timedelta(hours=8)))
        offsets = {"fashion": (18, 12, 7, 2, 0), "ink": (30, 22, 14, 5, 0)}
        for key, minutes in offsets.items():
            with self.subTest(style=key):
                content = TemplateContent(build_html(STYLE_BY_ID[key], None, stamp))
                expected = []
                for offset in minutes:
                    moment = stamp - timedelta(minutes=offset)
                    time_format = "%H:%M" if moment.date() == stamp.date() else "%m.%d %H:%M"
                    expected.append(moment.strftime(time_format))
                self.assertEqual(content.story_times, expected)
                self.assertEqual(content.story_times[-1], "00:10")
                self.assertTrue(all(value.count(":") == 1 for value in content.story_times))
                self.assertEqual(len(set(content.story_times)), 5)

    def test_api_payload_and_mocked_transport(self):
        image = sample_image()
        encoded = image_data_uri(image)
        decoded = QImage.fromData(base64.b64decode(encoded.split(",", 1)[1]))
        self.assertEqual(decoded.width(), 1280)
        self.assertGreater(decoded.pixelColor(100, 100).red(), decoded.pixelColor(100, 100).blue())
        self.assertGreater(decoded.pixelColor(1100, 100).blue(), decoded.pixelColor(1100, 100).red())
        source = ARTIFACTS / "transport-source.jpg"
        image.save(str(source), "JPEG", 92)
        reply = json.dumps({"data": [{"b64_json": base64.b64encode(source.read_bytes()).decode("ascii")}]}).encode()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return reply

        client = SeedreamClient("test-key", "test-model")
        with patch("services.urllib_request.urlopen", return_value=Response()) as request:
            output = client.generate(image, STYLE_BY_ID["qingcheng"], ARTIFACTS / "transport-result.png")
        sent = request.call_args.args[0]
        payload = json.loads(sent.data)
        self.assertEqual(sent.full_url, "https://ark.cn-beijing.volces.com/api/v3/images/generations")
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(len(payload["image"]), 1)
        self.assertEqual(payload["image"][0], encoded)
        self.assertEqual(payload["size"], "1728x2304")
        self.assertFalse(payload["watermark"])
        self.assertFalse(QImage(str(output)).isNull())
        self.assertTrue(output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_camera_crop_no_mirror_no_stale_edges(self):
        controller = CameraController()
        image = sample_image()
        controller._frame(SimpleNamespace(toImage=lambda: image))
        self.assertEqual(controller.last_frame.pixelColor(0, 0), image.pixelColor(0, 0))
        self.assertEqual(controller.last_frame.pixelColor(1919, 0), image.pixelColor(1919, 0))
        preview = CameraPreview()
        preview.resize(600, 900)
        preview.show()
        preview.set_frame(image)
        QTest.qWait(30)
        captured = preview.capture_frame(controller.last_frame)
        self.assertEqual(captured.size().width(), 720)
        self.assertEqual(captured.size().height(), 1080)
        self.assertEqual(captured.pixelColor(0, 100), image.pixelColor(600, 100))
        fresh = QImage(1920, 1080, QImage.Format_RGB32)
        fresh.fill(QColor("#f2c84b"))
        preview.set_frame(fresh)
        QTest.qWait(30)
        painted = preview.grab().toImage()
        for x in (2, painted.width() // 2, painted.width() - 3):
            self.assertEqual(painted.pixelColor(x, 100), QColor("#f2c84b"))
        preview.set_frame(QImage())
        QTest.qWait(30)
        self.assertEqual(preview.grab().toImage().pixelColor(2, 100), QColor("#111917"))
        preview.close()
        controller.deleteLater()

    def test_print_latch_and_safe_failures(self):
        window = self.window
        window.print_path = ARTIFACTS / "transport-source.png"
        with patch("ui.physical_printer", return_value="Test physical printer"), patch.object(window, "_start_worker") as start:
            window._print()
            window._print()
            self.assertEqual(start.call_count, 1)
            self.assertFalse(window.print_button.isEnabled())
            window._printed("submitted", "submitted")
            window._print()
            self.assertEqual(start.call_count, 1)
            self.assertTrue(window.print_locked)
            window._printed("unknown", "unknown")
            window._print()
            self.assertEqual(start.call_count, 1)
            self.assertEqual(window.print_button.text(), "请检查打印队列")
            window._printed("failed", "not submitted")
            self.assertTrue(window.print_button.isEnabled())
        for code, state in ((0, "submitted"), (2, "failed"), (3, "failed"), (4, "unknown"), (1, "unknown"), (-1, "unknown")):
            worker = PrintWorker(ARTIFACTS / "transport-source.png", "test", 1)
            states = []
            worker.finished.connect(lambda value, message: states.append(value))
            with patch("services.subprocess.run", return_value=SimpleNamespace(returncode=code, stdout=b"", stderr=b"")):
                worker.run()
            self.assertEqual(states, [state])

    def test_capture_waits_for_confirmation_and_ignores_late_frames(self):
        window = self.window
        with patch.object(window, "_generate") as generate:
            self.capture_photo()
            self.assertEqual(window.current_step, ui.CAMERA_STEP)
            self.assertEqual(window.max_step, ui.CAMERA_STEP)
            self.assertFalse(window.busy)
            self.assertFalse(window.photo_confirmed)
            self.assertFalse(window.current_photo.isNull())
            self.assertTrue(window.photo_use_button.isVisibleTo(window))
            self.assertTrue(window.photo_retake_button.isVisibleTo(window))
            self.assertFalse(window.capture_button.isVisibleTo(window))
            frozen = window.current_photo.copy()
            message = window.camera_message.text()
            window._camera_frame(sample_image(600, 800))
            window._camera_status("late camera status")
            window._begin_countdown()
            window._countdown_tick()
            window._jump(ui.RESULT_STEP)
            QTest.qWait(1350)
            self.assertEqual(window.current_photo, frozen)
            self.assertEqual(window.camera_preview.image, frozen)
            self.assertEqual(window.camera_message.text(), message)
            self.assertEqual(window.countdown, 0)
            self.assertEqual(window.current_step, ui.CAMERA_STEP)
            self.assertFalse(window.countdown_timer.isActive())
            self.assertFalse(window.threads)
            generate.assert_not_called()

    def test_retake_discards_unconfirmed_photo_without_generation(self):
        window = self.window
        window._select_style(4)
        with patch.object(window, "_generate") as generate:
            self.capture_photo()
            first_session = window.session_id
            with patch.object(window.camera, "start") as start:
                QTest.mouseClick(window.photo_retake_button, Qt.LeftButton)
            start.assert_called_once()
            self.assertNotEqual(window.session_id, first_session)
            self.assertEqual(window.selected_style.id, "chef")
            self.assertTrue(window.current_photo.isNull())
            self.assertTrue(window.camera.last_frame.isNull())
            self.assertFalse(window.photo_confirmed)
            self.assertFalse(window.photo_use_button.isVisibleTo(window))
            self.assertTrue(window.capture_button.isVisibleTo(window))
            self.assertFalse(window.capture_button.isEnabled())
            fresh = QImage(1920, 1080, QImage.Format_RGB32)
            fresh.fill(QColor("#2a8c53"))
            window.camera._frame(SimpleNamespace(toImage=lambda: fresh))
            window._begin_countdown()
            window.countdown = 1
            window._countdown_tick()
            self.assertEqual(window.current_photo.pixelColor(0, 0), QColor("#2a8c53"))
            generate.assert_not_called()

    def test_photo_confirmation_submits_once_and_keeps_captured_photo(self):
        window = self.window
        with patch.object(window, "_generate"):
            self.capture_photo()
        photo = window.current_photo.copy()
        try:
            with patch("ui.load_api_key", return_value="test-only"), patch.object(window, "_start_worker") as start:
                QTest.mouseClick(window.photo_use_button, Qt.LeftButton)
                window._confirm_photo()
                self.assertTrue(window.photo_confirmed)
                self.assertTrue(window.busy)
                self.assertEqual(window.current_step, ui.RESULT_STEP)
                self.assertEqual(start.call_count, 1)
                self.assertEqual(start.call_args.args[0].image, photo)
                self.assertFalse(window.print_button.isEnabled())
        finally:
            window.busy = False

    def test_generation_rejects_unconfirmed_photo(self):
        window = self.window
        window.current_photo = sample_image()
        try:
            with patch("ui.load_api_key", return_value="test-only") as key, patch.object(window, "_start_worker") as start:
                window._generate()
                window._retry_creation()
                key.assert_not_called()
                start.assert_not_called()
        finally:
            window.busy = False

    def test_review_preview_preserves_photo_when_resized(self):
        preview = CameraPreview()
        photo = sample_image(600, 900)
        preview.resize(1000, 500)
        preview.show()
        preview.set_photo(photo)
        QTest.qWait(30)
        painted = preview.grab().toImage()
        self.assertEqual(preview.image, photo)
        self.assertEqual(painted.pixelColor(2, 100), QColor("#111917"))
        self.assertEqual(painted.pixelColor(400, 100), photo.pixelColor(0, 0))
        self.assertEqual(painted.pixelColor(600, 100), photo.pixelColor(599, 0))
        preview.set_frame(sample_image(1000, 500))
        QTest.qWait(30)
        self.assertNotEqual(preview.grab().toImage().pixelColor(2, 100), QColor("#111917"))
        preview.close()

    def test_render_retry_reuses_existing_visual(self):
        window = self.window
        source = ARTIFACTS / "retry-visual.png"
        sample_image().save(str(source))
        window.generated_path = source
        with patch.object(window, "_render_final") as render, patch.object(window, "_generate") as generate:
            window._retry_creation()
        render.assert_called_once()
        generate.assert_not_called()

    def test_rotated_kiosk_geometry(self):
        inner = ui.BoothWindow()
        outer = ui.RotatedWindow(inner)
        outer.resize(1920, 1080)
        outer.show()
        QTest.qWait(150)
        viewport = outer.view.viewport().size()
        self.assertEqual((inner.width(), inner.height()), (viewport.height(), viewport.width()))
        mapped = outer.view.mapFromScene(outer.proxy.sceneBoundingRect()).boundingRect()
        self.assertGreater(mapped.width(), viewport.width() * 0.98)
        self.assertGreater(mapped.height(), viewport.height() * 0.98)
        outer.grab().save(str(ARTIFACTS / "rotated-1920x1080.png"))
        inner.renderer.shutdown()
        outer.close()
        outer.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_copy_stepper_and_print_lock(self):
        window = self.window
        window.print_path = ARTIFACTS / "synthetic-print.png"
        window._refresh_controls()
        self.assertFalse(window.copy_less.isEnabled())
        self.assertTrue(window.copy_more.isEnabled())
        window.copy_more.click()
        self.assertEqual(window.copy_spin.value(), 2)
        window.copy_spin.setValue(9)
        self.assertFalse(window.copy_more.isEnabled())
        self.assertTrue(window.copy_less.isEnabled())
        window.print_locked = True
        window._refresh_controls()
        self.assertFalse(window.copy_less.isEnabled())
        self.assertFalse(window.copy_more.isEnabled())
        self.assertFalse(window.copy_spin.isEnabled())

    def test_spinner_and_real_stage_states(self):
        window = self.window
        window._go(ui.RESULT_STEP)
        window.result_stack.setCurrentIndex(0)
        window.progress_phase = 1
        window._refresh_controls()
        QTest.qWait(50)
        before = window.creation_spinner.angle
        QTest.qWait(100)
        self.assertNotEqual(window.creation_spinner.angle, before)
        self.assertEqual([state.text() for state in window.stage_states], ["已完成", "进行中", "待开始"])
        window.progress_phase = 2
        window._refresh_controls()
        self.assertEqual([state.text() for state in window.stage_states], ["已完成", "已完成", "进行中"])
        window.result_stack.setCurrentIndex(1)
        self.assertFalse(window.creation_spinner.timer.isActive())

    def test_seven_templates_render_a4_at_300dpi(self):
        window = self.window
        results, errors = {}, []
        window.renderer.completed.connect(lambda key, pdf, png: results.update({key: (pdf, png)}))
        window.renderer.failed.connect(lambda key, message: errors.append((key, message)))
        source = ARTIFACTS / "all-styles-visual.png"
        sample_image(1200, 1600).save(str(source))
        for style in STYLES:
            window.renderer.submit(RenderJob(f"check:{style.id}", style, source, window.timestamp, 300))
        wait_for(lambda: len(results) + len(errors) == 7, 120000)
        self.assertEqual(errors, [])
        for key, (pdf, png) in results.items():
            self.assertTrue(Path(pdf).stat().st_size > 10000)
            image = QImage(png)
            self.assertEqual((image.width(), image.height()), (2480, 3508), key)

    def test_template_text_not_clipped_or_overlapping(self):
        for style in STYLES:
            page = QWebEnginePage(self.window.renderer.profile, self.window)
            loaded, report = [], []
            path = ARTIFACTS / f"geometry-{style.id}.html"
            path.write_text(build_html(style, None, self.window.timestamp), encoding="utf-8")
            page.loadFinished.connect(lambda ok: loaded.append(ok))
            page.load(QUrl.fromLocalFile(str(path)))
            wait_for(lambda: bool(loaded))
            self.assertTrue(loaded[0], style.id)
            QTest.qWait(100)
            page.runJavaScript(TEXT_GEOMETRY, lambda result: report.append(json.loads(result)))
            wait_for(lambda: bool(report))
            page.deleteLater()
            self.assertEqual(report[0]["clipped"], [], style.id)
            self.assertEqual(report[0]["overlaps"], [], style.id)
            self.assertEqual(report[0]["hiddenByContainer"], [], style.id)
            if style.id in ("fashion", "ink"):
                metadata = report[0]["metadata"]
                for removed in ("℃", "小雨", "晴", "天光", "硬光", "地点", "第 002 天"):
                    self.assertNotIn(removed, metadata)

    def test_ui_portrait_geometry(self):
        window = self.window
        for width, height in ((760, 1280), (1080, 1920), (760, 960)):
            window.resize(width, height)
            window._go(0)
            window._select_style(0)
            QTest.qWait(220)
            window.grab().save(str(ARTIFACTS / f"styles-{width}x{height}.png"))
            for thumb in window.cover_buttons:
                self.assertGreaterEqual(thumb.width(), 44)
                self.assertGreaterEqual(thumb.height(), 44)
            self.assertGreater(window.cover_canvas.height(), height * 0.4)
            photo = window.cover_canvas.image_rect()
            self.assertTrue(QRectF(window.cover_canvas.rect()).adjusted(-1, -1, 1, 1).contains(photo))
            self.assertAlmostEqual(photo.width() / photo.height(), window.cover_canvas.image.width() / window.cover_canvas.image.height())
            with patch.object(window.camera, "start"):
                window._confirm_style()
            QTest.qWait(40)
            window.grab().save(str(ARTIFACTS / f"camera-{width}x{height}.png"))
            window.camera._frame(SimpleNamespace(toImage=lambda: sample_image()))
            window._begin_countdown()
            window.countdown = 1
            window._countdown_tick()
            QTest.qWait(40)
            window.grab().save(str(ARTIFACTS / f"photo-review-{width}x{height}.png"))
            for widget in window.findChildren(QPushButton):
                if widget.isVisibleTo(window):
                    required = widget.fontMetrics().horizontalAdvance(widget.text()) + 40
                    if not widget.icon().isNull():
                        required += widget.iconSize().width() + 6
                    self.assertGreaterEqual(widget.width() + 2, required, widget.text())

        window.resize(760, 1280)
        window._go(ui.STYLE_STEP)
        for index, style in enumerate(STYLES):
            window._select_style(index)
            QTest.qWait(220)
            window.grab().save(str(ARTIFACTS / f"cover-{style.id}.png"))
        window._select_style(0)

    def test_workflow_without_api_or_camera_hardware(self):
        window = self.window
        window.resize(760, 1280)
        window._select_style(0)

        def generate(client, image, style, output):
            time.sleep(0.25)
            output.parent.mkdir(parents=True, exist_ok=True)
            image.save(str(output))
            return output

        with patch.object(window.camera, "start"), patch("ui.load_api_key", return_value="test-only"), patch.object(SeedreamClient, "generate", generate):
            window._confirm_style()
            self.assertEqual(window.current_step, ui.CAMERA_STEP)
            window.camera._frame(SimpleNamespace(toImage=lambda: sample_image()))
            window._begin_countdown()
            window.countdown = 1
            window._countdown_tick()
            self.assertFalse(window.busy)
            self.assertEqual(window.current_step, ui.CAMERA_STEP)
            self.assertFalse(window.photo_confirmed)
            QTest.mouseClick(window.photo_use_button, Qt.LeftButton)
            self.assertTrue(window.busy)
            self.assertEqual(window.current_step, ui.RESULT_STEP)
            self.assertFalse(window.print_button.isEnabled())
            QTest.qWait(60)
            window.grab().save(str(ARTIFACTS / "generating.png"))
            wait_for(lambda: not window.busy)
            wait_for(lambda: not window.threads)
        self.assertEqual(window.result_stack.currentIndex(), 1, window.error_label.text())
        self.assertTrue(window.pdf_path.is_file())
        self.assertTrue(window.print_path.is_file())
        self.assertTrue(window.print_button.isEnabled())
        self.assertLess(window.current_photo.width(), 1920)
        QTest.qWait(60)
        window.grab().save(str(ARTIFACTS / "result-760x1280.png"))
        window.resize(1080, 1920)
        QTest.qWait(60)
        window.grab().save(str(ARTIFACTS / "result-1080x1920.png"))


if __name__ == "__main__":
    print(f"Test artifacts: {ARTIFACTS}", flush=True)
    unittest.main(verbosity=2)
