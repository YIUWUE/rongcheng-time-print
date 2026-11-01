from __future__ import annotations

import base64
import json
import ssl
import subprocess
import sys
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, Signal, Qt
from PySide6.QtGui import QImage, QPainter, QPageLayout, QPageSize
from PySide6.QtPrintSupport import QPrinter, QPrinterInfo

from catalog import CityStyle, build_prompt
from paths import APP_DIR, SEEDREAM_MODEL, SEEDREAM_URL, log


def image_data_uri(image: QImage) -> str:
    prepared = image.convertToFormat(QImage.Format_RGB888)
    if max(prepared.width(), prepared.height()) > 1280:
        prepared = prepared.scaled(1280, 1280, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    if not prepared.save(buffer, "JPEG", 92):
        raise RuntimeError("照片编码失败，请重新拍摄。")
    buffer.close()
    return "data:image/jpeg;base64," + base64.b64encode(bytes(data)).decode("ascii")


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class SeedreamClient:
    def __init__(self, api_key: str, model: str = SEEDREAM_MODEL) -> None:
        self.api_key = api_key
        self.model = model

    def payload(self, image: QImage, style: CityStyle) -> dict:
        return {
            "model": self.model,
            "prompt": build_prompt(style),
            "size": style.size,
            "response_format": "url",
            "n": 1,
            "watermark": False,
            "image": [image_data_uri(image)],
        }

    def generate(self, image: QImage, style: CityStyle, output: Path) -> Path:
        payload = self.payload(image, style)
        request = urllib_request.Request(
            SEEDREAM_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        log(f"generation start style={style.id} model={self.model} size={style.size}")
        try:
            with urllib_request.urlopen(request, context=ssl_context(), timeout=240) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(detail).get("error", {}).get("message", "")
            except (ValueError, AttributeError):
                message = ""
            if error.code in (401, 403):
                raise RuntimeError("AI 服务认证失败，请检查 HUOSHAN_API_KEY 和模型权限。") from error
            raise RuntimeError(f"AI 服务返回 {error.code}：{message[:400] or '请稍后重试。'}") from error
        except (urllib_error.URLError, TimeoutError) as error:
            raise RuntimeError("AI 服务网络连接失败或超时，请检查网络后重试。") from error
        data = result.get("data") or []
        if isinstance(data, list) and data:
            entry = data[0]
        elif isinstance(data, dict):
            entry = data
        else:
            raise RuntimeError("AI 服务没有返回图片。")
        encoded = entry.get("b64_json") or entry.get("image_base64")
        url = entry.get("url") or entry.get("image_url")
        output.parent.mkdir(parents=True, exist_ok=True)
        if encoded:
            output.write_bytes(base64.b64decode(encoded))
        elif url:
            with urllib_request.urlopen(url, context=ssl_context(), timeout=120) as response:
                output.write_bytes(response.read())
        else:
            raise RuntimeError("AI 服务返回内容中缺少图片地址。")
        generated = QImage(str(output))
        if generated.isNull():
            raise RuntimeError("生成图片无法读取，请重新生成。")
        if not generated.save(str(output), "PNG"):
            raise RuntimeError("生成图片保存失败，请检查运行目录的可写权限。")
        log(f"generation complete style={style.id}")
        return output


class GenerationWorker(QObject):
    finished = Signal(bool, str)

    def __init__(self, image: QImage, style: CityStyle, api_key: str, output: Path) -> None:
        super().__init__()
        self.image = image.copy()
        self.style = style
        self.api_key = api_key
        self.output = output

    def run(self) -> None:
        try:
            path = SeedreamClient(self.api_key).generate(self.image, self.style, self.output)
            self.finished.emit(True, str(path))
        except Exception as error:
            log(f"generation failed {type(error).__name__}: {error}")
            self.finished.emit(False, str(error))


def physical_printer() -> str:
    printer = QPrinterInfo.defaultPrinter()
    if printer.isNull():
        raise RuntimeError("未找到默认打印机，请检查驱动和连接。")
    name = printer.printerName()
    if any(token in name.lower() for token in ("pdf", "xps", "onenote", "fax", "save as", "保存", "另存")):
        raise RuntimeError("请将默认打印机设置为实体打印机；电子版可使用导出 PDF。")
    return name


class PrintWorker(QObject):
    finished = Signal(str, str)

    def __init__(self, image_path: Path, printer: str, copies: int) -> None:
        super().__init__()
        self.image_path = image_path
        self.printer = printer
        self.copies = max(1, min(copies, 9))

    def run(self) -> None:
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.append(str(APP_DIR / "main.py"))
        command += ["--print-helper", str(self.image_path), self.printer, str(self.copies)]
        try:
            result = subprocess.run(command, capture_output=True, timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode == 0:
                self.finished.emit("submitted", f"已提交 {self.copies} 份，请在出纸口等候。")
            elif result.returncode in (2, 3):
                text = (result.stdout or result.stderr).decode("utf-8", errors="replace").strip()
                self.finished.emit("failed", text[-400:] or "打印提交失败，请检查打印机。")
            else:
                self.finished.emit("unknown", "打印提交结果尚未确认，请先检查打印队列，避免重复打印。")
        except subprocess.TimeoutExpired:
            self.finished.emit("unknown", "打印提交结果尚未确认，请先检查打印队列，避免重复打印。")
        except OSError as error:
            self.finished.emit("failed", f"无法启动打印服务：{error}")


def print_image(path: str, printer_name: str, copies: int) -> int:
    image = QImage(path)
    if image.isNull():
        print("打印图读取失败。", flush=True)
        return 2
    printer = QPrinter(QPrinter.HighResolution)
    printer.setPrinterName(printer_name)
    printer.setDocName("Rongcheng Time Print")
    printer.setResolution(300)
    printer.setPageSize(QPageSize(QPageSize.A4))
    printer.setPageOrientation(QPageLayout.Portrait)
    printer.setCopyCount(max(1, min(copies, 9)))
    painter = QPainter()
    if not painter.begin(printer):
        print("打印机不可用，未能提交任务。", flush=True)
        return 3
    rect = printer.pageRect(QPrinter.DevicePixel)
    scaled = image.scaled(round(rect.width()), round(rect.height()), Qt.KeepAspectRatio, Qt.SmoothTransformation)
    painter.drawImage(round(rect.x() + (rect.width() - scaled.width()) / 2), round(rect.y() + (rect.height() - scaled.height()) / 2), scaled)
    if not painter.end():
        print("打印任务提交结果未知，请检查打印队列。", flush=True)
        return 4
    print("打印任务已提交。", flush=True)
    return 0
