"""Очередь треков: скачивает, отдаёт в Soundpad, ждёт окончания, играет следующий."""
from __future__ import annotations

import itertools
import json
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .soundpad import Soundpad, SoundpadError
from .sources import Downloader, SourceError, Track, resolve


def app_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "SoundpadMusic")
    os.makedirs(path, exist_ok=True)
    return path


@dataclass
class Settings:
    play_in_mic: bool = True        # то, ради чего всё затевалось
    play_in_speakers: bool = True   # слышать самому
    normalize: bool = True          # выравнивать громкость
    autoremove: bool = True         # убирать трек из списка Soundpad после проигрывания
    cookies_browser: str = ""       # chrome / firefox / edge / ... для VK и т.п.

    @classmethod
    def load(cls, path: str) -> "Settings":
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: str) -> None:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        except OSError:
            pass


QUEUED, DOWNLOADING, READY, PLAYING, DONE, ERROR = (
    "в очереди", "скачивается", "готов", "играет", "сыграл", "ошибка")

_ids = itertools.count(1)


@dataclass
class QueueItem:
    track: Track
    id: int = field(default_factory=lambda: next(_ids))
    status: str = QUEUED
    error: str = ""
    path: str = ""
    progress: float = 0.0
    future: Optional[Future] = None
    removed: bool = False

    @property
    def title(self) -> str:
        return self.track.title


EventCallback = Callable[[str, dict], None]


class Player:
    """Все обращения к Soundpad идут из одного рабочего потока (+ кнопки управления)."""

    POLL = 0.4
    START_TIMEOUT = 6.0

    def __init__(self, soundpad: Soundpad, downloader: Downloader, settings: Settings,
                 on_event: Optional[EventCallback] = None,
                 resolver: Callable[[str, str], list[Track]] = resolve):
        self.sp = soundpad
        self.dl = downloader
        self.settings = settings
        self._on_event = on_event or (lambda kind, data: None)
        self._resolver = resolver

        self.items: list[QueueItem] = []
        self._cond = threading.Condition()
        self._skip = threading.Event()
        self._shutdown = False
        self._current: Optional[QueueItem] = None
        self._dl_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="download")
        self._thread = threading.Thread(target=self._run, name="player", daemon=True)

    # --- публичное API --------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def shutdown(self, timeout: float = 3.0) -> None:
        with self._cond:
            self._shutdown = True
            self._cond.notify_all()
        self._skip.set()
        self._thread.join(timeout)
        self._dl_pool.shutdown(wait=False, cancel_futures=True)

    def add(self, text: str) -> None:
        """Ссылка или запрос; разбор идёт в фоне, чтобы не вешать интерфейс."""
        def work():
            self._emit("status", text=f"Ищу: {text}")
            try:
                tracks = self._resolver(text, self.settings.cookies_browser)
            except SourceError as e:
                self._emit("error", text=str(e))
                return
            except Exception as e:
                self._emit("error", text=f"Ошибка: {e}")
                return
            if not tracks:
                self._emit("error", text="Ничего не найдено")
                return
            self.enqueue(tracks)
            self._emit("status", text=f"Добавлено треков: {len(tracks)}")
        threading.Thread(target=work, daemon=True).start()

    def enqueue(self, tracks: list[Track]) -> list[QueueItem]:
        new = [QueueItem(t) for t in tracks]
        with self._cond:
            self.items.extend(new)
            self._cond.notify_all()
        self._emit("queue")
        return new

    def skip(self) -> None:
        self._skip.set()

    def stop(self) -> None:
        """Остановить текущий трек и очистить очередь."""
        with self._cond:
            for it in self.items:
                if it.status in (QUEUED, DOWNLOADING, READY):
                    it.removed = True
            self.items = [it for it in self.items if not it.removed]
        self._skip.set()
        self._emit("queue")

    def remove(self, item_id: int) -> None:
        with self._cond:
            for it in self.items:
                if it.id == item_id:
                    if it is self._current:
                        self._skip.set()
                    else:
                        it.removed = True
            self.items = [it for it in self.items if not it.removed]
        self._emit("queue")

    def clear_finished(self) -> None:
        with self._cond:
            self.items = [it for it in self.items if it.status not in (DONE, ERROR)]
        self._emit("queue")

    def toggle_pause(self) -> None:
        try:
            self.sp.toggle_pause()
        except SoundpadError as e:
            self._emit("error", text=str(e))

    def snapshot(self) -> list[QueueItem]:
        with self._cond:
            return list(self.items)

    # --- внутренности -------------------------------------------------------
    def _emit(self, kind: str, **data) -> None:
        try:
            self._on_event(kind, data)
        except Exception:
            pass

    def _set(self, item: QueueItem, status: str, error: str = "") -> None:
        item.status = status
        item.error = error
        self._emit("queue")

    def _download(self, item: QueueItem) -> str:
        if item.removed:
            raise SourceError("удалён")
        self._set(item, DOWNLOADING)

        def progress(p: float) -> None:
            item.progress = p
            self._emit("item_progress", id=item.id, progress=p)

        path, title = self.dl.download(item.track, normalize=self.settings.normalize,
                                       cookies_browser=self.settings.cookies_browser,
                                       progress=progress)
        item.path = path
        if title:
            item.track.title = title
        if item.status == DOWNLOADING:
            self._set(item, READY)
        return path

    def _ensure_future(self, item: QueueItem) -> Future:
        if item.future is None:
            item.future = self._dl_pool.submit(self._download, item)
        return item.future

    def _next_pending(self) -> Optional[QueueItem]:
        for it in self.items:
            if not it.removed and it.status in (QUEUED, DOWNLOADING, READY):
                return it
        return None

    def _prefetch(self) -> None:
        with self._cond:
            nxt = self._next_pending()
        if nxt is not None:
            self._ensure_future(nxt)

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._shutdown and self._next_pending() is None:
                    self._cond.wait()
                if self._shutdown:
                    return
                item = self._next_pending()
                fut = self._ensure_future(item)
            try:
                path = fut.result()
            except Exception as e:
                if not item.removed:
                    self._set(item, ERROR, str(e))
                    self._emit("error", text=f"{item.title}: {e}")
                else:
                    item.status = DONE
                continue
            if item.removed or self._shutdown:
                continue
            self._skip.clear()
            self._current = item
            try:
                self._play(item, path)
            except SoundpadError as e:
                self._set(item, ERROR, str(e))
                self._emit("error", text=str(e))
            finally:
                self._current = None
                self._emit("now_playing", item=None)

    def _play(self, item: QueueItem, path: str) -> None:
        index = self.sp.add_sound(path)
        self.sp.play(index, speakers=self.settings.play_in_speakers,
                     mic=self.settings.play_in_mic)
        self._set(item, PLAYING)
        self._emit("now_playing", item=item)
        self._prefetch()  # пока играет этот, качаем следующий

        # ждём, пока Soundpad реально начнёт играть
        deadline = time.monotonic() + self.START_TIMEOUT
        started = False
        while not self._skip.is_set() and time.monotonic() < deadline:
            if self.sp.get_status() != Soundpad.STOPPED:
                started = True
                break
            self._skip.wait(0.15)

        if started:
            while not self._skip.is_set() and not self._shutdown:
                status = self.sp.get_status()
                if status == Soundpad.STOPPED:
                    break
                self._emit("progress", position=self.sp.get_position_ms(),
                           duration=self.sp.get_duration_ms(),
                           paused=status == Soundpad.PAUSED)
                self._skip.wait(self.POLL)

        skipped = self._skip.is_set()
        if skipped:
            self._skip.clear()
            try:
                self.sp.stop()
            except SoundpadError:
                pass

        if not started and not skipped and not self._shutdown:
            self._set(item, ERROR, "Soundpad не начал воспроизведение")
        else:
            self._set(item, DONE)

        if self.settings.autoremove:
            try:
                self.sp.remove_sound(path)
            except SoundpadError:
                pass
