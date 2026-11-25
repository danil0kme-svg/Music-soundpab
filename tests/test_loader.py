import hashlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from soundpad_music import ytdlp_loader as L  # noqa: E402


def touch(d, name):
    p = os.path.join(d, name)
    open(p, "wb").close()
    return p


class LoaderTest(unittest.TestCase):
    def setUp(self):
        self.a, self.b = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.old_path = list(sys.path)

    def tearDown(self):
        sys.path[:] = self.old_path
        L._loaded_version = None

    def test_picks_newest_across_dirs(self):
        touch(self.a, "yt_dlp-2025.9.1-py3-none-any.whl")
        newest = touch(self.b, "yt_dlp-2025.10.22-py3-none-any.whl")
        touch(self.b, "yt_dlp-2025.10.3-py3-none-any.whl")
        self.assertEqual(L.setup([self.a, self.b]), "2025.10.22")
        self.assertEqual(sys.path[0], newest)

    def test_not_frozen_does_nothing(self):
        self.assertIsNone(L.setup())

    def make_fetch(self, version, blob, sha=None):
        meta = {"info": {"version": version}, "urls": [
            {"packagetype": "sdist", "filename": "x.tar.gz", "url": "s", "digests": {"sha256": ""}},
            {"packagetype": "bdist_wheel", "filename": f"yt_dlp-{version}-py3-none-any.whl",
             "url": "wheel-url", "digests": {"sha256": sha or hashlib.sha256(blob).hexdigest()}}]}
        def fetch(url):
            return json.dumps(meta).encode() if url == L.PYPI_URL else blob
        return fetch

    def test_update_downloads_and_prunes(self):
        touch(self.a, "yt_dlp-2025.1.1-py3-none-any.whl")       # старая, удалится
        touch(self.a, "yt_dlp-2025.10.22-py3-none-any.whl")     # подключена сейчас
        L._loaded_version = "2025.10.22"
        v = L.update(self.a, self.make_fetch("2026.11.23", b"WHEEL"))
        self.assertEqual(v, "2026.11.23")
        self.assertEqual(sorted(os.listdir(self.a)),
                         ["yt_dlp-2025.10.22-py3-none-any.whl",
                          "yt_dlp-2026.11.23-py3-none-any.whl"])

    def test_update_noop_when_current(self):
        L._loaded_version = "2026.11.23"
        self.assertIsNone(L.update(self.a, self.make_fetch("2026.11.23", b"X")))

    def test_bad_checksum_rejected(self):
        L._loaded_version = "2025.1.1"
        with self.assertRaises(RuntimeError):
            L.update(self.a, self.make_fetch("2026.1.1", b"EVIL", sha="00"))
        self.assertEqual(os.listdir(self.a), [])


if __name__ == "__main__":
    unittest.main()
