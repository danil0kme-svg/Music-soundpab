"""Простое окно на tkinter."""
from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import log as logmod
from .log import log
from .player import DONE, DOWNLOADING, ERROR, PLAYING, Player, Settings, app_dir
from .soundpad import Soundpad
from .sources import Downloader

BROWSERS = ["", "chrome", "edge", "firefox", "opera", "brave", "vivaldi", "chromium"]


def _fmt_ms(ms: int) -> str:
    s = max(ms, 0) // 1000
    return f"{s // 60}:{s % 60:02d}"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Soundpad Music — музыка в микрофон")
        root.geometry("720x600")
        root.minsize(520, 440)
        root.report_callback_exception = self._on_tk_error

        self.settings_path = os.path.join(app_dir(), "settings.json")
        self.settings = Settings.load(self.settings_path)
        self.events: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self.player = Player(Soundpad(), Downloader(os.path.join(app_dir(), "cache")),
                             self.settings, on_event=lambda k, d: self.events.put((k, d)))

        self._build()
        self.player.start()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._pump)
        root.after(300, self._check_soundpad)
        log.info("Окно запущено")
        from . import ytdlp_loader
        ytdlp_loader.update_in_background(
            lambda msg: self.events.put(("status", {"text": msg})))

    # --- разметка -----------------------------------------------------------
    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Ссылка (YouTube / VK / Яндекс Музыка / SoundCloud…) или название:"
                  ).pack(anchor="w")
        row = ttk.Frame(top)
        row.pack(fill="x", pady=(2, 0))
        self.entry = ttk.Entry(row)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self._add())
        self.entry.focus_set()
        ttk.Button(row, text="▶ В микрофон", command=self._add).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="Вставить", command=self._paste).pack(side="left", padx=(6, 0))

        self.now_var = tk.StringVar(value="Ничего не играет")
        ttk.Label(self.root, textvariable=self.now_var, font=("Segoe UI", 10, "bold")
                  ).pack(fill="x", **pad)
        self.progress = ttk.Progressbar(self.root, mode="determinate", maximum=1000)
        self.progress.pack(fill="x", padx=8)

        ctl = ttk.Frame(self.root)
        ctl.pack(fill="x", **pad)
        ttk.Button(ctl, text="⏯ Пауза", command=self.player.toggle_pause).pack(side="left")
        ttk.Button(ctl, text="⏭ Следующий", command=self.player.skip).pack(side="left", padx=4)
        ttk.Button(ctl, text="⏹ Стоп всё", command=self.player.stop).pack(side="left")
        ttk.Button(ctl, text="Убрать выбранный", command=self._remove_selected
                   ).pack(side="right")
        ttk.Button(ctl, text="Убрать сыгранные", command=self.player.clear_finished
                   ).pack(side="right", padx=4)

        cols = ("status", "title")
        self.tree = ttk.Treeview(self.root, columns=cols, show="headings", height=6)
        self.tree.heading("status", text="Статус")
        self.tree.heading("title", text="Трек")
        self.tree.column("status", width=110, stretch=False)
        self.tree.column("title", width=480)
        self.tree.tag_configure("playing", foreground="#0a7d28")
        self.tree.tag_configure("error", foreground="#b00020")
        self.tree.tag_configure("done", foreground="#888888")

        # Низ окна пакуем раньше списка, чтобы при нехватке места сжимался список,
        # а не пропадала строка состояния с ошибками
        bottom = ttk.Frame(self.root)
        bottom.pack(side="bottom", fill="x", padx=8, pady=(0, 6))
        self.sp_var = tk.StringVar(value="Soundpad: проверяю…")
        self.sp_label = tk.Label(bottom, textvariable=self.sp_var, anchor="w")
        self.sp_label.pack(fill="x")
        self.status_var = tk.StringVar(value="Вставьте ссылку или название и нажмите «▶ В микрофон»")
        self.status_label = tk.Label(bottom, textvariable=self.status_var, anchor="w",
                                     justify="left", wraplength=680,
                                     font=("Segoe UI", 10, "bold"))
        self.status_label.pack(fill="x")

        opts = ttk.LabelFrame(self.root, text="Настройки")
        opts.pack(side="bottom", fill="x", **pad)
        self.vars = {}
        for i, (key, label) in enumerate([
                ("play_in_mic", "Играть в микрофон"),
                ("play_in_speakers", "Слышать самому"),
                ("normalize", "Выравнивать громкость"),
                ("autoremove", "Убирать трек из Soundpad после проигрывания")]):
            v = tk.BooleanVar(value=getattr(self.settings, key))
            v.trace_add("write", lambda *_a, k=key, var=v: self._set_opt(k, var.get()))
            self.vars[key] = v
            ttk.Checkbutton(opts, text=label, variable=v).grid(
                row=i // 2, column=i % 2, sticky="w", padx=6, pady=2)
        brow = ttk.Frame(opts)
        brow.grid(row=2, column=0, columnspan=2, sticky="w", padx=6, pady=(2, 6))
        ttk.Label(brow, text="Cookies из браузера (нужно для VK):").pack(side="left")
        self.browser = ttk.Combobox(brow, values=BROWSERS, width=12, state="readonly")
        self.browser.set(self.settings.cookies_browser)
        self.browser.bind("<<ComboboxSelected>>",
                          lambda e: self._set_opt("cookies_browser", self.browser.get()))
        self.browser.pack(side="left", padx=6)
        ttk.Button(brow, text="Очистить кэш", command=self._clear_cache).pack(side="left", padx=6)
        ttk.Button(brow, text="Открыть лог", command=self._open_log).pack(side="left")

        self.tree.pack(fill="both", expand=True, padx=8, pady=4)

    # --- действия -----------------------------------------------------------
    def _add(self) -> None:
        text = self.entry.get().strip()
        if text:
            self.player.add(text)
            self.entry.delete(0, "end")

    def _paste(self) -> None:
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return
        self.entry.delete(0, "end")
        self.entry.insert(0, text.strip())
        self._add()

    def _remove_selected(self) -> None:
        for iid in self.tree.selection():
            self.player.remove(int(iid))

    def _set_opt(self, key: str, value) -> None:
        setattr(self.settings, key, value)
        self.settings.save(self.settings_path)

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status_var.set(("⚠ " if error else "") + text)
        self.status_label.configure(fg="#c62828" if error else "#1b5e20")

    def _open_log(self) -> None:
        p = logmod.path()
        if not os.path.exists(p):
            self._set_status("Лог пока пуст")
            return
        try:
            if sys.platform == "win32":
                os.startfile(p)  # type: ignore[attr-defined]
            else:
                self._set_status(p)
        except OSError as e:
            self._set_status(f"Не открыть лог: {e}", error=True)

    def _on_tk_error(self, exc, val, tb) -> None:
        log.error("Ошибка в окне", exc_info=(exc, val, tb))
        messagebox.showerror("Soundpad Music",
                             f"Ошибка: {exc.__name__}: {val}\n\nПодробности в логе:\n{logmod.path()}")

    def _clear_cache(self) -> None:
        n = self.player.dl.clear_cache()
        self._set_status(f"Удалено файлов из кэша: {n}")

    def _check_soundpad(self) -> None:
        """Каждые 5 секунд проверяем связь с Soundpad и показываем её внизу окна."""
        def work():
            try:
                ok = self.player.sp.is_alive()
            except Exception:
                log.exception("Проверка Soundpad")
                ok = False
            self.events.put(("soundpad", {"ok": ok}))
        threading.Thread(target=work, daemon=True).start()
        self.root.after(5000, self._check_soundpad)

    # --- события от плеера --------------------------------------------------
    def _pump(self) -> None:
        try:
            self._pump_once()
        except Exception:
            log.exception("Ошибка обработки событий")
        finally:
            self.root.after(100, self._pump)  # что бы ни случилось, окно продолжает жить

    def _pump_once(self) -> None:
        refresh = False
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind in ("queue", "item_progress"):
                    refresh = True
                elif kind == "soundpad":
                    if data.get("ok"):
                        self.sp_var.set("Soundpad: ✅ подключен")
                        self.sp_label.configure(fg="#1b5e20")
                    else:
                        self.sp_var.set("Soundpad: ❌ не найден. Запустите Soundpad "
                                        "(нужна полная версия, не демо)")
                        self.sp_label.configure(fg="#c62828")
                elif kind == "status":
                    self._set_status(data.get("text", ""))
                elif kind == "error":
                    text = data.get("text", "")
                    self._set_status(text.splitlines()[0] if text else "Ошибка", error=True)
                    if "\n" in text:
                        messagebox.showwarning("Soundpad Music", text)
                elif kind == "now_playing":
                    item = data.get("item")
                    self.now_var.set(f"▶ {item.title}" if item else "Ничего не играет")
                    if not item:
                        self.progress["value"] = 0
                elif kind == "progress":
                    dur = data.get("duration") or 0
                    pos = data.get("position") or 0
                    if dur:
                        self.progress["value"] = pos * 1000 / dur
                    title = self.now_var.get().split("   ")[0]
                    mark = "⏸" if data.get("paused") else ""
                    self.now_var.set(f"{title}   {_fmt_ms(pos)} / {_fmt_ms(dur)} {mark}")
        except queue.Empty:
            pass
        if refresh:
            self._refresh_tree()

    def _refresh_tree(self) -> None:
        items = self.player.snapshot()
        ids = {str(it.id) for it in items}
        for iid in self.tree.get_children():
            if iid not in ids:
                self.tree.delete(iid)
        for pos, it in enumerate(items):
            status = it.status
            if status == DOWNLOADING and it.progress:
                status = f"{status} {int(it.progress * 100)}%"
            title = it.title + (f" — {it.error}" if it.status == ERROR and it.error else "")
            tag = {PLAYING: "playing", ERROR: "error", DONE: "done"}.get(it.status, "")
            iid = str(it.id)
            if self.tree.exists(iid):
                self.tree.item(iid, values=(status, title), tags=(tag,))
                self.tree.move(iid, "", pos)
            else:
                self.tree.insert("", pos, iid=iid, values=(status, title), tags=(tag,))

    def _on_close(self) -> None:
        self.player.shutdown()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
