"""Превращает то, что ввёл пользователь (ссылку или название), в список треков,
и скачивает трек в mp3, пригодный для Soundpad.

* YouTube, VK-видео, SoundCloud и ещё ~1000 сайтов — напрямую через yt-dlp.
* Яндекс Музыка — берём «исполнитель — название» из публичного API Яндекса
  и ищем трек на YouTube (без подписки Яндекс отдаёт только 30-секундные превью).
* Всё остальное (Spotify, VK-аудио без cookies и т.п.) — если yt-dlp не смог,
  берём заголовок страницы и ищем его на YouTube.
* Просто текст — поиск на YouTube.
"""
from __future__ import annotations

import glob
import html
import json
import os
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

_URL_RE = re.compile(r"^https?://", re.I)
_YA_TRACK_RE = re.compile(r"music\.yandex\.[a-z]+/(?:album/\d+/)?track/(\d+)", re.I)
_YA_ALBUM_RE = re.compile(r"music\.yandex\.[a-z]+/album/(\d+)/?(?:[?#]|$)", re.I)
_YA_PLAYLIST_RE = re.compile(r"music\.yandex\.[a-z]+/users/([^/?#]+)/playlists/(\d+)", re.I)

# Хвосты, которые сайты дописывают к заголовкам страниц
_TITLE_JUNK = [
    r"\s*[—–-]\s*слушать онлайн.*$",
    r"\s*\|\s*VK.*$",
    r"\s*\|\s*ВКонтакте.*$",
    r"\s*\|\s*Spotify.*$",
    r"\s*-\s*song and lyrics by\s*",
    r"\s*-\s*song by\s*",
    r"\s*on Apple Music.*$",
    r"\s*\|\s*Deezer.*$",
]
_USELESS_TITLES = {"", "вконтакте", "vk", "вход", "войти", "log in", "spotify",
                   "яндекс музыка", "яндекс.музыка", "youtube"}

Progress = Callable[[float], None]


class SourceError(RuntimeError):
    pass


@dataclass
class Track:
    title: str
    url: str  # то, что отдаём yt-dlp: ссылка или "ytsearch1:запрос"

    @staticmethod
    def search(query: str, title: Optional[str] = None) -> "Track":
        return Track(title or query, f"ytsearch1:{query}")


def is_url(text: str) -> bool:
    return bool(_URL_RE.match(text.strip()))


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _http_get(url: str, timeout: float = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept-Language": "ru,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_json(url: str) -> dict:
    return json.loads(_http_get(url).decode("utf-8"))


# --------------------------------------------------------------------------
# Яндекс Музыка
# --------------------------------------------------------------------------
def _ya_track_name(t: dict) -> Optional[str]:
    title = (t.get("title") or "").strip()
    if not title:
        return None
    if t.get("version"):
        title = f"{title} ({t['version']})"
    artists = ", ".join(a.get("name", "") for a in t.get("artists") or [] if a.get("name"))
    return f"{artists} - {title}" if artists else title


def _ya_tracks_to_list(raw: list) -> list[Track]:
    out = []
    for t in raw:
        if isinstance(t, dict) and "track" in t and isinstance(t["track"], dict):
            t = t["track"]
        name = _ya_track_name(t) if isinstance(t, dict) else None
        if name:
            out.append(Track.search(name))
    return out


def resolve_yandex(url: str, fetch_json: Callable[[str], dict] = _http_json) -> Optional[list[Track]]:
    """Возвращает треки для ссылки Яндекс Музыки или None, если это не она."""
    api = "https://api.music.yandex.net"
    m = _YA_TRACK_RE.search(url)
    if m:
        data = fetch_json(f"{api}/tracks/{m.group(1)}")
        return _ya_tracks_to_list(data.get("result") or [])
    m = _YA_ALBUM_RE.search(url)
    if m:
        data = fetch_json(f"{api}/albums/{m.group(1)}/with-tracks")
        volumes = (data.get("result") or {}).get("volumes") or []
        return _ya_tracks_to_list([t for vol in volumes for t in vol])
    m = _YA_PLAYLIST_RE.search(url)
    if m:
        data = fetch_json(f"{api}/users/{m.group(1)}/playlists/{m.group(2)}")
        return _ya_tracks_to_list((data.get("result") or {}).get("tracks") or [])
    return None


# --------------------------------------------------------------------------
# Заголовок страницы как запасной вариант
# --------------------------------------------------------------------------
def clean_page_title(title: str) -> str:
    title = html.unescape(title).strip()
    for pat in _TITLE_JUNK:
        title = re.sub(pat, " ", title, flags=re.I).strip()
    return re.sub(r"\s+", " ", title)


def extract_page_title(page: str) -> Optional[str]:
    for pat in (r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title',
                r"<title[^>]*>([^<]+)</title>"):
        m = re.search(pat, page, re.I)
        if m:
            t = clean_page_title(m.group(1))
            if t.lower() not in _USELESS_TITLES and len(t) > 2:
                return t
    return None


def resolve_by_page_title(url: str, fetch: Callable[[str], bytes] = _http_get) -> Optional[Track]:
    try:
        page = fetch(url).decode("utf-8", errors="replace")
    except Exception:
        return None
    title = extract_page_title(page)
    return Track.search(title) if title else None


# --------------------------------------------------------------------------
# yt-dlp
# --------------------------------------------------------------------------
def find_ffmpeg() -> Optional[str]:
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _ydl_opts(cookies_browser: str = "", **extra) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _QuietLogger(),
        "noplaylist": False,
        "socket_timeout": 20,
    }
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    opts.update(extra)
    return opts


def _ytdlp_resolve(url: str, cookies_browser: str = "") -> list[Track]:
    import yt_dlp  # type: ignore

    with yt_dlp.YoutubeDL(_ydl_opts(cookies_browser, extract_flat="in_playlist")) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return []
    if info.get("_type") == "playlist" or info.get("entries"):
        tracks = []
        for e in info.get("entries") or []:
            if not e:
                continue
            link = e.get("url") or e.get("webpage_url")
            if not link:
                continue
            tracks.append(Track(e.get("title") or link, link))
        return tracks
    return [Track(info.get("title") or url, info.get("webpage_url") or url)]


def resolve(text: str, cookies_browser: str = "") -> list[Track]:
    """Ссылка/запрос -> список треков. Бросает SourceError, если ничего не нашли."""
    text = text.strip()
    if not text:
        raise SourceError("Пустой запрос")
    if not is_url(text):
        return [Track.search(text)]

    try:
        ya = resolve_yandex(text)
    except Exception:
        ya = None
    if ya:
        return ya

    error = None
    try:
        tracks = _ytdlp_resolve(text, cookies_browser)
        if tracks:
            return tracks
    except Exception as e:  # yt_dlp.utils.DownloadError и прочее
        error = e

    fallback = resolve_by_page_title(text)
    if fallback:
        return [fallback]
    hint = ("Не получилось достать трек по ссылке. Попробуйте выбрать браузер "
            "в «Cookies из браузера» (для VK нужен вход) или просто введите "
            "«исполнитель - название».")
    if error:
        hint += f"\n\n{_short_error(error)}"
    raise SourceError(hint)


def _short_error(e: Exception) -> str:
    msg = re.sub(r"^(\x1b\[[0-9;]*m)?ERROR:(\x1b\[0m)?\s*", "", str(e).strip())
    return msg.splitlines()[0][:300] if msg else e.__class__.__name__


# --------------------------------------------------------------------------
# Скачивание
# --------------------------------------------------------------------------
def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:120]


class Downloader:
    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        self.raw_dir = os.path.join(cache_dir, "raw")
        os.makedirs(self.raw_dir, exist_ok=True)

    def clear_cache(self) -> int:
        removed = 0
        for f in glob.glob(os.path.join(self.cache_dir, "*.mp3")) + \
                glob.glob(os.path.join(self.raw_dir, "*")):
            try:
                os.remove(f)
                removed += 1
            except OSError:
                pass  # файл может быть занят Soundpad'ом
        return removed

    def download(self, track: Track, normalize: bool = True, cookies_browser: str = "",
                 progress: Optional[Progress] = None) -> tuple[str, str]:
        """Скачивает трек. Возвращает (путь к mp3, настоящее название)."""
        import yt_dlp  # type: ignore

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise SourceError("Не найден ffmpeg. Установите его или выполните "
                              "pip install imageio-ffmpeg")

        def hook(d):
            if progress and d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total:
                    progress(min(d.get("downloaded_bytes", 0) / total, 1.0))

        opts = _ydl_opts(
            cookies_browser,
            format="bestaudio/best",
            outtmpl=os.path.join(self.raw_dir, "%(extractor_key)s_%(id)s.%(ext)s"),
            progress_hooks=[hook],
            ffmpeg_location=ffmpeg,
            noplaylist=True,
            overwrites=True,
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(track.url, download=False)
                if info and (info.get("_type") == "playlist" or "entries" in info):
                    entries = [e for e in info.get("entries") or [] if e]
                    if not entries:
                        raise SourceError(f"Ничего не найдено: {track.title}")
                    info = entries[0]
                if info.get("is_live"):
                    raise SourceError("Прямые трансляции не поддерживаются")
                key = _safe(f"{info.get('extractor_key', 'x')}_{info.get('id', 'x')}")
                title = info.get("title") or track.title
                out = os.path.join(self.cache_dir, f"{key}{'_n' if normalize else ''}.mp3")
                if os.path.exists(out):
                    return out, title
                ydl.extract_info(info.get("webpage_url") or track.url, download=True)
        except SourceError:
            raise
        except Exception as e:
            raise SourceError(_short_error(e))

        raws = [p for p in glob.glob(os.path.join(self.raw_dir, glob.escape(key) + ".*"))
                if not p.endswith((".part", ".ytdl"))]
        if not raws:
            raise SourceError("yt-dlp ничего не скачал")
        self._convert(ffmpeg, raws[0], out, normalize)
        for r in raws:
            try:
                os.remove(r)
            except OSError:
                pass
        return out, title

    @staticmethod
    def _convert(ffmpeg: str, src: str, dst: str, normalize: bool) -> None:
        tmp = dst + ".tmp.mp3"
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", src,
               "-vn", "-ac", "2", "-ar", "48000"]
        if normalize:
            # выравниваем громкость, чтобы в микрофоне не было то тихо, то громко
            cmd += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
        cmd += ["-b:a", "192k", tmp]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            raise SourceError(f"ffmpeg: {r.stderr.strip()[:300]}")
        os.replace(tmp, dst)
