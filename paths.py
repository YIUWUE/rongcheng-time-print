from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

APP_NAME = "蓉城时光印"
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
DATA_DIR = Path(os.environ.get("RONGCHENG_DATA_DIR", str(APP_DIR / "runtime"))).resolve()
SEEDREAM_URL = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
SEEDREAM_MODEL = os.environ.get("HUOSHAN_SEEDREAM_MODEL", "doubao-seedream-5-0-lite-260128")
CAPTURE_SECONDS = 5
RETURN_SECONDS = 60


def log(message: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (DATA_DIR / "runtime.log").open("a", encoding="utf-8") as stream:
        stream.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")


def load_api_key() -> str:
    key = os.environ.get("HUOSHAN_API_KEY", "").strip()
    if key:
        return key
    try:
        import winreg
    except ImportError:
        return ""
    for hive, subkey in ((winreg.HKEY_CURRENT_USER, "Environment"), (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(hive, subkey) as registry:
                value, _ = winreg.QueryValueEx(registry, "HUOSHAN_API_KEY")
                if value:
                    return str(value).strip()
        except OSError:
            pass
    return ""
