"""Запуск:
    python -m soundpad_music                  — окно
    python -m soundpad_music <ссылка|название> [...] — из консоли, без окна
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from .player import DONE, ERROR, Player, Settings, app_dir
from .soundpad import Soundpad
from .sources import Downloader, SourceError, resolve


def cli(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="soundpad_music",
                                description="Проиграть музыку через Soundpad в микрофон")
    p.add_argument("items", nargs="+", help="ссылки или названия треков")
    p.add_argument("--no-speakers", action="store_true", help="не слышать самому")
    p.add_argument("--no-normalize", action="store_true", help="не выравнивать громкость")
    p.add_argument("--keep", action="store_true", help="не удалять треки из списка Soundpad")
    p.add_argument("--cookies", default="", metavar="BROWSER",
                   help="брать cookies из браузера (chrome, edge, firefox...) — нужно для VK")
    args = p.parse_args(argv)

    settings = Settings(play_in_speakers=not args.no_speakers, normalize=not args.no_normalize,
                        autoremove=not args.keep, cookies_browser=args.cookies)
    sp = Soundpad()
    if not sp.is_alive():
        print("Soundpad не запущен.", file=sys.stderr)
        return 1

    def on_event(kind, data):
        if kind in ("status", "error"):
            print(("! " if kind == "error" else "") + data.get("text", ""), flush=True)
        elif kind == "now_playing" and data.get("item"):
            print(f"▶ {data['item'].title}", flush=True)

    player = Player(sp, Downloader(os.path.join(app_dir(), "cache")), settings, on_event)
    player.start()
    for text in args.items:
        try:
            player.enqueue(resolve(text, settings.cookies_browser))
        except SourceError as e:
            print(f"! {e}", file=sys.stderr)
    try:
        while any(it.status not in (DONE, ERROR) for it in player.snapshot()):
            time.sleep(0.5)
    except KeyboardInterrupt:
        player.stop()
    player.shutdown()
    return 0


def _bootstrap() -> None:
    # В оконном exe нет консоли: sys.stdout = None, а yt-dlp туда заглядывает
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    from . import ytdlp_loader
    ytdlp_loader.setup()


def main() -> None:
    _bootstrap()
    if sys.argv[1:] == ["--version"]:
        from . import __version__
        import yt_dlp.version
        print(f"Soundpad Music {__version__}, yt-dlp {yt_dlp.version.__version__} "
              f"({os.path.dirname(os.path.dirname(yt_dlp.version.__file__))})")
        from .sources import find_ffmpeg
        print(f"ffmpeg: {find_ffmpeg() or 'не найден'}")
        return
    if sys.argv[1:] == ["--update"]:
        from . import ytdlp_loader
        if not ytdlp_loader.is_frozen():
            print("Это для exe. Из исходников: pip install -U yt-dlp")
            return
        v = ytdlp_loader.update()
        print(f"yt-dlp обновлён до {v}" if v else "yt-dlp уже свежий")
        return
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    from .gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
