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


def main() -> None:
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    from .gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
