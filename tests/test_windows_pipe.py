"""Проверка настоящего WinAPI-канала: поднимаем поддельный «Soundpad» на named pipe
и общаемся с ним тем же кодом, что и с настоящим. Только для Windows (идёт в CI)."""
import os
import sys
import threading
import time
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from soundpad_music.soundpad import (Soundpad, SoundpadNotRunning,  # noqa: E402
                                     SoundpadTimeout, WindowsPipeTransport)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_core import FakeSoundpadTransport  # noqa: E402


class PipeServer:
    """Сервер в режиме сообщений, как у Soundpad. handler(str) -> str | None (None = молчать)."""

    def __init__(self, name, handler):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        k = self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, wintypes.LPVOID]
        k.CreateNamedPipeW.restype = wintypes.HANDLE
        k.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        k.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.name, self.handler = name, handler
        self.stop = False
        threading.Thread(target=self._accept_loop, daemon=True).start()
        time.sleep(0.2)

    def _accept_loop(self):
        while not self.stop:
            # PIPE_ACCESS_DUPLEX, PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE
            h = self.k.CreateNamedPipeW(self.name, 3, 4 | 2, 255, 65536, 65536, 0, None)
            ok = self.k.ConnectNamedPipe(h, None)
            if not ok and self.ct.get_last_error() != 535:  # ERROR_PIPE_CONNECTED
                self.k.CloseHandle(h)
                continue
            threading.Thread(target=self._serve, args=(h,), daemon=True).start()

    def _serve(self, h):
        ct, wt = self.ct, self.wt
        buf = ct.create_string_buffer(65536)
        while True:
            n = wt.DWORD(0)
            if not self.k.ReadFile(h, buf, len(buf), ct.byref(n), None):
                break
            reply = self.handler(buf.raw[:n.value].decode("utf-8"))
            if reply is None:
                continue
            data = reply.encode("utf-8")
            w = wt.DWORD(0)
            if not self.k.WriteFile(h, data, len(data), ct.byref(w), None):
                break
        self.k.DisconnectNamedPipe(h)
        self.k.CloseHandle(h)


@unittest.skipUnless(sys.platform == "win32", "только Windows")
class WindowsPipeTest(unittest.TestCase):
    def setUp(self):
        self.name = rf"\\.\pipe\sp_test_{uuid.uuid4().hex}"
        self.fake = FakeSoundpadTransport()

    def handler(self, cmd):
        if cmd == "Hang()":
            return None
        if cmd == "Big()":
            return "x" * 200_000  # больше буфера — проверяем ERROR_MORE_DATA
        return self.fake.send(cmd)

    def test_full_protocol_over_real_pipe(self):
        srv = PipeServer(self.name, self.handler)
        self.addCleanup(setattr, srv, "stop", True)
        sp = Soundpad(WindowsPipeTransport(self.name, reply_timeout=1.0))
        self.assertTrue(sp.is_alive())
        idx = sp.add_sound(r"C:\music\song.mp3")
        self.assertEqual(idx, 2)
        sp.play(idx, speakers=True, mic=True)
        self.assertEqual(sp.get_status(), "PLAYING")
        self.assertTrue(sp.remove_sound(r"C:\music\song.mp3"))
        self.assertIn("DoPlaySound(2, true, true)", self.fake.log)

        self.assertEqual(len(sp.transport.send("Big()")), 200_000)

        t0 = time.monotonic()
        with self.assertRaises(SoundpadTimeout):
            sp.transport.send("Hang()")
        self.assertLess(time.monotonic() - t0, 5)
        self.assertTrue(sp.is_alive())  # после таймаута переподключились и работаем
        sp.close()

    def test_not_running(self):
        with self.assertRaises(SoundpadNotRunning):
            WindowsPipeTransport(self.name).send("IsAlive()")
        self.assertFalse(Soundpad(WindowsPipeTransport(self.name)).is_alive())


if __name__ == "__main__":
    unittest.main()
