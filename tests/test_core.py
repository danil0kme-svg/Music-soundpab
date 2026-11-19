import os
import re
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from soundpad_music import sources  # noqa: E402
from soundpad_music.player import DONE, ERROR, Player, Settings  # noqa: E402
from soundpad_music.soundpad import Soundpad  # noqa: E402
from soundpad_music.sources import SourceError, Track  # noqa: E402


class FakeSoundpadTransport:
    """Эмулирует протокол Soundpad: список звуков, проигрывание, статус."""

    def __init__(self, play_ticks=3):
        self.sounds = [r"C:\Sounds\airhorn.mp3"]
        self.selected = None
        self.playing = None
        self.ticks_left = 0
        self.play_ticks = play_ticks
        self.log = []
        self.lock = threading.Lock()

    def close(self):
        pass

    def send(self, cmd):
        with self.lock:
            self.log.append(cmd)
            return self._handle(cmd)

    def _handle(self, cmd):
        if cmd == "IsAlive()":
            return "R-200"
        if cmd == "GetSoundFileCount()":
            return str(len(self.sounds))
        if cmd == "GetSoundlist()":
            body = "".join(f'<Sound index="{i + 1}" url="{p}" title="t{i}"/>'
                           for i, p in enumerate(self.sounds))
            return f"<Soundlist>{body}</Soundlist>"
        m = re.fullmatch(r'DoAddSound\("(.+)"\)', cmd)
        if m:
            self.sounds.append(m.group(1))
            return "R-200"
        m = re.fullmatch(r"DoPlaySound\((\d+), (true|false), (true|false)\)", cmd)
        if m:
            self.playing = int(m.group(1))
            self.ticks_left = self.play_ticks
            return "R-200"
        m = re.fullmatch(r"DoSelectIndex\((\d+)\)", cmd)
        if m:
            self.selected = int(m.group(1))
            return "R-200"
        if cmd == "DoRemoveSelectedEntries(false)":
            del self.sounds[self.selected - 1]
            return "R-200"
        if cmd == "DoStopSound()":
            self.playing = None
            return "R-200"
        if cmd == "DoTogglePause()":
            return "R-200"
        if cmd == "GetPlayStatus()":
            if self.playing is None:
                return "STOPPED"
            self.ticks_left -= 1
            if self.ticks_left < 0:
                self.playing = None
                return "STOPPED"
            return "PLAYING"
        if cmd == "GetPlaybackPositionInMs()":
            return "1000"
        if cmd == "GetPlaybackDurationInMs()":
            return "3000"
        return "R-404: unknown"


class FakeDownloader:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = []

    def download(self, track, normalize=True, cookies_browser="", progress=None):
        self.calls.append(track.url)
        if track.title in self.fail:
            raise SourceError("не скачалось")
        if progress:
            progress(1.0)
        return rf"C:\cache\{track.title}.mp3", track.title.upper()


def wait_until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class SoundpadClientTest(unittest.TestCase):
    def test_add_finds_index_and_reuses_existing(self):
        t = FakeSoundpadTransport()
        sp = Soundpad(t)
        self.assertEqual(sp.add_sound(r"C:\a.mp3"), 2)
        self.assertEqual(sp.add_sound("c:/A.mp3"), 2)  # тот же файл — не дублируем
        self.assertEqual(len(t.sounds), 2)

    def test_play_and_remove(self):
        t = FakeSoundpadTransport()
        sp = Soundpad(t)
        idx = sp.add_sound(r"C:\a.mp3")
        sp.play(idx, speakers=False, mic=True)
        self.assertIn("DoPlaySound(2, false, true)", t.log)
        self.assertTrue(sp.remove_sound(r"C:\a.mp3"))
        self.assertEqual(t.sounds, [r"C:\Sounds\airhorn.mp3"])
        self.assertFalse(sp.remove_sound(r"C:\nope.mp3"))

    def test_error_response_raises(self):
        class Bad(FakeSoundpadTransport):
            def _handle(self, cmd):
                return "R-500: nope"
        from soundpad_music.soundpad import SoundpadError
        with self.assertRaises(SoundpadError):
            Soundpad(Bad()).stop()


class PlayerTest(unittest.TestCase):
    def make(self, fail=(), autoremove=True):
        t = FakeSoundpadTransport()
        events = []
        player = Player(Soundpad(t), FakeDownloader(fail), Settings(autoremove=autoremove),
                        on_event=lambda k, d: events.append((k, d)))
        player.POLL = 0.01
        player.start()
        self.addCleanup(player.shutdown)
        return player, t, events

    def test_plays_queue_in_order_and_cleans_up(self):
        player, t, events = self.make()
        player.enqueue([Track("one", "u1"), Track("two", "u2")])
        self.assertTrue(wait_until(lambda: all(i.status == DONE for i in player.snapshot())))
        plays = [c for c in t.log if c.startswith("DoPlaySound")]
        self.assertEqual(len(plays), 2)
        self.assertEqual(t.sounds, [r"C:\Sounds\airhorn.mp3"])  # чужие звуки не тронуты
        self.assertEqual([i.title for i in player.snapshot()], ["ONE", "TWO"])
        self.assertTrue(any(k == "progress" for k, _ in events))

    def test_keeps_sounds_when_autoremove_off(self):
        player, t, _ = self.make(autoremove=False)
        player.enqueue([Track("one", "u1")])
        self.assertTrue(wait_until(lambda: player.snapshot()[0].status == DONE))
        self.assertEqual(len(t.sounds), 2)

    def test_failed_download_is_skipped(self):
        player, t, events = self.make(fail={"bad"})
        player.enqueue([Track("bad", "u0"), Track("good", "u1")])
        self.assertTrue(wait_until(lambda: [i.status for i in player.snapshot()] == [ERROR, DONE]))
        self.assertTrue(any(k == "error" for k, _ in events))

    def test_skip_stops_current(self):
        player, t, _ = self.make()
        t.play_ticks = 10_000
        player.enqueue([Track("long", "u1"), Track("next", "u2")])
        self.assertTrue(wait_until(lambda: player.snapshot()[0].status == "играет"))
        player.skip()
        self.assertTrue(wait_until(lambda: player.snapshot()[1].status == "играет"))
        self.assertIn("DoStopSound()", t.log)
        player.stop()
        self.assertTrue(wait_until(lambda: player.snapshot()[1].status == DONE))

    def test_stop_clears_pending(self):
        player, t, _ = self.make()
        t.play_ticks = 10_000
        player.enqueue([Track("a", "u1"), Track("b", "u2"), Track("c", "u3")])
        self.assertTrue(wait_until(lambda: player.snapshot()[0].status == "играет"))
        player.stop()
        self.assertTrue(wait_until(lambda: [i.status for i in player.snapshot()] == [DONE]))

    def test_add_uses_resolver(self):
        t = FakeSoundpadTransport()
        player = Player(Soundpad(t), FakeDownloader(), Settings(),
                        resolver=lambda text, cookies: [Track.search(text)])
        player.POLL = 0.01
        player.start()
        self.addCleanup(player.shutdown)
        player.add("Кино - Группа крови")
        self.assertTrue(wait_until(lambda: player.snapshot() and
                                   player.snapshot()[0].status == DONE))
        self.assertEqual(player.snapshot()[0].track.url, "ytsearch1:Кино - Группа крови")


class SourcesTest(unittest.TestCase):
    def test_plain_text_is_youtube_search(self):
        self.assertEqual(sources.resolve("  Rick Astley  "),
                         [Track("Rick Astley", "ytsearch1:Rick Astley")])

    def test_yandex_track(self):
        def fake(url):
            self.assertEqual(url, "https://api.music.yandex.net/tracks/12345")
            return {"result": [{"title": "Группа крови",
                                "artists": [{"name": "Кино"}]}]}
        res = sources.resolve_yandex("https://music.yandex.ru/album/1/track/12345?utm=x", fake)
        self.assertEqual(res, [Track.search("Кино - Группа крови")])

    def test_yandex_album_and_playlist(self):
        def fake(url):
            if "/albums/7/" in url:
                return {"result": {"volumes": [[{"title": "A", "artists": [{"name": "X"}]}],
                                               [{"title": "B", "artists": [],
                                                 "version": "Remix"}]]}}
            if "/users/bob/playlists/3" in url:
                return {"result": {"tracks": [{"track": {"title": "C",
                                                         "artists": [{"name": "Y"},
                                                                     {"name": "Z"}]}}]}}
            raise AssertionError(url)
        self.assertEqual([t.title for t in sources.resolve_yandex(
            "https://music.yandex.ru/album/7", fake)], ["X - A", "B (Remix)"])
        self.assertEqual([t.title for t in sources.resolve_yandex(
            "https://music.yandex.com/users/bob/playlists/3", fake)], ["Y, Z - C"])
        self.assertIsNone(sources.resolve_yandex("https://youtube.com/watch?v=1", fake))

    def test_page_title_fallback(self):
        page = ('<html><head><meta property="og:title" content="Numb - song and lyrics by '
                'Linkin Park | Spotify"></head></html>')
        self.assertEqual(sources.extract_page_title(page), "Numb Linkin Park")
        self.assertIsNone(sources.extract_page_title("<title>ВКонтакте</title>"))
        self.assertEqual(
            sources.extract_page_title("<title>Звери — Районы-кварталы — слушать онлайн</title>"),
            "Звери — Районы-кварталы")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(sources.find_ffmpeg(), "нужен ffmpeg")
class DownloaderTest(unittest.TestCase):
    """Проверяем путь скачивание -> ffmpeg -> кэш с поддельным yt_dlp."""

    def setUp(self):
        import subprocess
        import tempfile
        import types
        self.tmp = tempfile.mkdtemp()
        ffmpeg = sources.find_ffmpeg()
        calls = self.calls = []

        class FakeYDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                calls.append((url, download))
                info = {"extractor_key": "Youtube", "id": "abc123", "title": "Real Title",
                        "webpage_url": "https://youtu.be/abc123"}
                if download:
                    out = self.opts["outtmpl"] % {**info, "ext": "webm"}
                    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                                    "sine=frequency=440:duration=1", out], check=True)
                    for h in self.opts["progress_hooks"]:
                        h({"status": "downloading", "downloaded_bytes": 5, "total_bytes": 10})
                    return info
                return {"_type": "playlist", "entries": [info]}

        fake = types.ModuleType("yt_dlp")
        fake.YoutubeDL = FakeYDL
        self._old = sys.modules.get("yt_dlp")
        sys.modules["yt_dlp"] = fake

    def tearDown(self):
        import shutil
        if self._old is None:
            sys.modules.pop("yt_dlp", None)
        else:
            sys.modules["yt_dlp"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_download_convert_and_cache(self):
        d = sources.Downloader(self.tmp)
        prog = []
        path, title = d.download(Track.search("q"), progress=prog.append)
        self.assertEqual(title, "Real Title")
        self.assertTrue(path.endswith("Youtube_abc123_n.mp3"))
        self.assertGreater(os.path.getsize(path), 1000)
        self.assertEqual(os.listdir(d.raw_dir), [])  # исходник удалён
        self.assertEqual(prog, [0.5])
        # второй раз — из кэша, без скачивания
        self.calls.clear()
        self.assertEqual(d.download(Track.search("q"))[0], path)
        self.assertEqual(self.calls, [("ytsearch1:q", False)])
        self.assertEqual(d.clear_cache(), 1)
