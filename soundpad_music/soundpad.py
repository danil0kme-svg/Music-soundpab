"""Клиент удалённого управления Soundpad.

Soundpad слушает именованный канал ``\\\\.\\pipe\\sp_remote_control``.
Команда — это строка вида ``DoPlaySound(5, true, true)``; ответ — либо
``R-200`` (успех), либо ``R-<код>: <текст>`` (ошибка), либо значение
для команд ``Get*``.
"""
from __future__ import annotations

import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional, Protocol

PIPE_NAME = r"\\.\pipe\sp_remote_control"


class SoundpadError(RuntimeError):
    pass


class SoundpadNotRunning(SoundpadError):
    def __init__(self, msg: str = "Soundpad не запущен (или недоступно удалённое управление)"):
        super().__init__(msg)


class Transport(Protocol):
    def send(self, command: str) -> str: ...

    def close(self) -> None: ...


class WindowsPipeTransport:
    """Транспорт через WinAPI: канал Soundpad работает в режиме сообщений,
    поэтому читаем через ReadFile до конца сообщения (ERROR_MORE_DATA)."""

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _OPEN_EXISTING = 3
    _PIPE_READMODE_MESSAGE = 0x2
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_PIPE_BUSY = 231
    _ERROR_MORE_DATA = 234

    def __init__(self, pipe_name: str = PIPE_NAME, connect_timeout: float = 2.0):
        if sys.platform != "win32":
            raise SoundpadError("Soundpad работает только в Windows")
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        self._wt = wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        k32.WaitNamedPipeW.restype = wintypes.BOOL
        k32.SetNamedPipeHandleState.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
                                                wintypes.LPVOID, wintypes.LPVOID]
        k32.SetNamedPipeHandleState.restype = wintypes.BOOL
        k32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k32.WriteFile.restype = wintypes.BOOL
        k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k32.ReadFile.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        self._k32 = k32
        self._invalid = wintypes.HANDLE(-1).value

        self._pipe_name = pipe_name
        self._timeout = connect_timeout
        self._handle = None
        self._lock = threading.Lock()

    # --- соединение ---------------------------------------------------------
    def _connect(self) -> None:
        deadline = time.monotonic() + self._timeout
        while True:
            h = self._k32.CreateFileW(self._pipe_name,
                                      self._GENERIC_READ | self._GENERIC_WRITE,
                                      0, None, self._OPEN_EXISTING, 0, None)
            if h and h != self._invalid:
                break
            err = self._ct.get_last_error()
            if err == self._ERROR_FILE_NOT_FOUND:
                raise SoundpadNotRunning()
            if err == self._ERROR_PIPE_BUSY and time.monotonic() < deadline:
                self._k32.WaitNamedPipeW(self._pipe_name, 200)
                continue
            raise SoundpadError(f"Не удалось подключиться к Soundpad (WinError {err})")
        mode = self._wt.DWORD(self._PIPE_READMODE_MESSAGE)
        self._k32.SetNamedPipeHandleState(h, self._ct.byref(mode), None, None)
        self._handle = h

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        if self._handle is not None:
            self._k32.CloseHandle(self._handle)
            self._handle = None

    # --- обмен --------------------------------------------------------------
    def _roundtrip(self, data: bytes) -> bytes:
        ct, wt = self._ct, self._wt
        written = wt.DWORD(0)
        if not self._k32.WriteFile(self._handle, data, len(data), ct.byref(written), None):
            raise OSError(ct.get_last_error(), "WriteFile failed")
        chunks = []
        buf = ct.create_string_buffer(65536)
        while True:
            read = wt.DWORD(0)
            ok = self._k32.ReadFile(self._handle, buf, len(buf), ct.byref(read), None)
            chunks.append(buf.raw[: read.value])
            if ok:
                break
            err = ct.get_last_error()
            if err != self._ERROR_MORE_DATA:
                raise OSError(err, "ReadFile failed")
        return b"".join(chunks)

    def send(self, command: str) -> str:
        data = command.encode("utf-8")
        with self._lock:
            for attempt in range(2):
                if self._handle is None:
                    self._connect()
                try:
                    raw = self._roundtrip(data)
                    return raw.decode("utf-8", errors="replace").rstrip("\x00")
                except OSError:
                    # Soundpad перезапустили или канал оборвался — переподключаемся
                    self._close_unlocked()
                    if attempt:
                        raise SoundpadNotRunning()
        raise SoundpadNotRunning()


@dataclass
class SoundEntry:
    index: int
    path: str
    title: str = ""


def _norm_path(p: str) -> str:
    return p.replace("/", "\\").rstrip("\\").lower()


def _bool(v: bool) -> str:
    return "true" if v else "false"


class Soundpad:
    """Высокоуровневые команды Soundpad, нужные плееру."""

    PLAYING = "PLAYING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    SEEKING = "SEEKING"

    def __init__(self, transport: Optional[Transport] = None):
        self._transport = transport

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            self._transport = WindowsPipeTransport()
        return self._transport

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()

    # --- базовое ------------------------------------------------------------
    def _query(self, command: str) -> str:
        return self.transport.send(command).strip()

    def _do(self, command: str) -> None:
        resp = self._query(command)
        if not resp.startswith("R-200"):
            raise SoundpadError(f"Soundpad отклонил команду {command}: {resp}")

    def is_alive(self) -> bool:
        try:
            return self._query("IsAlive()").startswith("R-200")
        except SoundpadError:
            return False

    # --- список звуков -------------------------------------------------------
    def get_sound_count(self) -> int:
        resp = self._query("GetSoundFileCount()")
        try:
            return int(resp)
        except ValueError:
            raise SoundpadError(f"Неожиданный ответ Soundpad: {resp}")

    def get_sounds(self) -> list[SoundEntry]:
        resp = self._query("GetSoundlist()")
        if not resp or resp.startswith("R-"):
            return []
        try:
            root = ET.fromstring(resp)
        except ET.ParseError as e:
            raise SoundpadError(f"Не удалось разобрать список звуков: {e}")
        result = []
        for el in root.iter("Sound"):
            try:
                idx = int(el.get("index", "0"))
            except ValueError:
                continue
            result.append(SoundEntry(idx, el.get("url", ""), el.get("title", "")))
        return result

    def find_index(self, path: str) -> Optional[int]:
        target = _norm_path(path)
        for s in self.get_sounds():
            if _norm_path(s.path) == target:
                return s.index
        return None

    def add_sound(self, path: str) -> int:
        """Добавляет файл в конец списка (если его там ещё нет) и возвращает индекс."""
        existing = self.find_index(path)
        if existing is not None:
            return existing
        self._do(f'DoAddSound("{path}")')
        idx = self.find_index(path)
        if idx is None:
            idx = self.get_sound_count()
        return idx

    def remove_sound(self, path: str) -> bool:
        idx = self.find_index(path)
        if idx is None:
            return False
        self._do(f"DoSelectIndex({idx})")
        self._do("DoRemoveSelectedEntries(false)")
        return True

    # --- воспроизведение -----------------------------------------------------
    def play(self, index: int, speakers: bool = True, mic: bool = True) -> None:
        self._do(f"DoPlaySound({index}, {_bool(speakers)}, {_bool(mic)})")

    def stop(self) -> None:
        self._do("DoStopSound()")

    def toggle_pause(self) -> None:
        self._do("DoTogglePause()")

    def get_status(self) -> str:
        return self._query("GetPlayStatus()").upper()

    def get_position_ms(self) -> int:
        try:
            return int(self._query("GetPlaybackPositionInMs()"))
        except ValueError:
            return 0

    def get_duration_ms(self) -> int:
        try:
            return int(self._query("GetPlaybackDurationInMs()"))
        except ValueError:
            return 0
