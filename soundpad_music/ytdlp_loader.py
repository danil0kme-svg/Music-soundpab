"""Самообновление yt-dlp для собранного exe.

YouTube регулярно ломает yt-dlp, поэтому в exe он не зашит намертво:
внутри лежит wheel-файл yt-dlp, а более свежие версии скачиваются с PyPI
в %APPDATA%\\SoundpadMusic\\lib. При запуске подключается самая новая
из имеющихся (wheel — это zip, Python импортирует из него напрямую).
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sys
import threading
import urllib.request
from typing import Callable, Optional

PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"

_loaded_version: Optional[str] = None


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def lib_dir() -> str:
    from .player import app_dir
    path = os.path.join(app_dir(), "lib")
    os.makedirs(path, exist_ok=True)
    return path


def _version_key(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def _wheel_version(path: str) -> Optional[str]:
    m = re.match(r"yt_dlp-([0-9][0-9.]*?)-py", os.path.basename(path))
    return m.group(1) if m else None


def _candidates(extra_dirs: list[str]) -> list[tuple[str, str]]:
    out = []
    for d in extra_dirs:
        for w in glob.glob(os.path.join(d, "yt_dlp-*.whl")):
            v = _wheel_version(w)
            if v:
                out.append((v, w))
    out.sort(key=lambda vw: _version_key(vw[0]), reverse=True)
    return out


def bundled_dir() -> Optional[str]:
    base = getattr(sys, "_MEIPASS", None)
    return os.path.join(base, "wheels") if base else None


def setup(dirs: Optional[list[str]] = None) -> Optional[str]:
    """Подключает самый свежий wheel yt-dlp. Возвращает его версию или None."""
    global _loaded_version
    if dirs is None:
        if not is_frozen():
            return None  # из исходников пользуемся установленным через pip
        dirs = [lib_dir()] + [d for d in [bundled_dir()] if d]
    cands = _candidates(dirs)
    if not cands:
        return None
    version, path = cands[0]
    if path not in sys.path:
        sys.path.insert(0, path)
    _loaded_version = version
    return version


def loaded_version() -> Optional[str]:
    return _loaded_version


def _fetch_latest(fetch: Callable[[str], bytes]) -> tuple[str, str, str, str]:
    data = json.loads(fetch(PYPI_URL).decode("utf-8"))
    version = data["info"]["version"]
    for f in data.get("urls") or []:
        if f.get("packagetype") == "bdist_wheel" and f["filename"].endswith(".whl"):
            return version, f["url"], f["digests"]["sha256"], f["filename"]
    raise RuntimeError("на PyPI нет wheel для yt-dlp")


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "SoundpadMusic-updater"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def update(target_dir: Optional[str] = None,
           fetch: Callable[[str], bytes] = _http_get) -> Optional[str]:
    """Скачивает свежий yt-dlp, если он новее подключённого.
    Возвращает новую версию (вступит в силу после перезапуска) или None."""
    target_dir = target_dir or lib_dir()
    current = _loaded_version or "0"
    version, url, sha256, filename = _fetch_latest(fetch)
    if _version_key(version) <= _version_key(current):
        return None
    dst = os.path.join(target_dir, os.path.basename(filename))
    if not os.path.exists(dst):
        blob = fetch(url)
        if hashlib.sha256(blob).hexdigest() != sha256:
            raise RuntimeError("контрольная сумма yt-dlp не совпала, обновление отменено")
        tmp = dst + ".part"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, dst)
    # старые версии убираем, кроме подключённой сейчас (из неё ещё идут импорты)
    for v, w in _candidates([target_dir]):
        if v not in (version, current):
            try:
                os.remove(w)
            except OSError:
                pass
    return version


def update_in_background(on_result: Callable[[str], None]) -> None:
    if not is_frozen():
        return

    def work():
        try:
            v = update()
        except Exception:
            return  # нет интернета и т.п. — не страшно, работаем на том, что есть
        if v:
            on_result(f"Скачивалка обновлена до {v} — перезапустите программу, "
                      "чтобы она подхватилась")
    threading.Thread(target=work, daemon=True).start()
