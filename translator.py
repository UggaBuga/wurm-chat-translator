#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Wurm Chat Translator — переводчик чата Wurm Online в реальном времени.

Как это работает
----------------
Wurm Online пишет все сообщения чата в текстовые логи:

    <Дом>\wurm\players\<Игрок>\logs\_Local.2026-10.txt
    <Дом>\wurm\players\<Игрок>\logs\_Kingdom.2026-10.txt
    <Дом>\wurm\players\<Игрок>\logs\_PM.<Игрок>.txt
    ... и т.д.

Эта программа следит за новыми строками в выбранных каналах, переводит их
через бесплатный Google Translate (конечная точка client=gtx, ключ не нужен)
и показывает перевод в отдельном окне поверх игры. Клиент игры не изменяется.

Запуск
------
    python translator.py            # оконный режим (по умолчанию)
    python translator.py --console  # вывод в консоль (для отладки)

Настройки сохраняются в config.json рядом со скриптом.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "Wurm Chat Translator"
CONFIG_FILE = Path(__file__).with_name("config.json")

# Каналы чата Wurm Online (имена файлов логов: _<Канал>.<год-месяц>.txt)
CHANNELS = ["Local", "Village", "Kingdom", "Freedom", "Trade", "Global", "PM"]

LANGUAGES = {
    "ru": "Русский", "en": "English", "de": "Deutsch", "fr": "Français",
    "es": "Español", "pt": "Português", "pl": "Polski", "tr": "Türkçe",
    "nl": "Nederlands", "it": "Italiano", "uk": "Українська",
}

# Обычные каналы:  [21:42:03] PlayerName: сообщение
# Личка (PM):      ["21:42:03"] <FromPlayer> сообщение / ["21:42:03"] >ToPlayer< сообщение
# Имя игрока в Wurm не содержит пробелов — поэтому строки вида
# "[21:42:03] You create a..." (системные, с пробелом до двоеточия) не сматчатся.
RE_CHAT = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\] ([^\s:\[\]]{2,30}): (.*\S)\s*$")
RE_PM = re.compile(r'^\["(\d{2}:\d{2}:\d{2})"\] ([<>])([^<>\[\]]{2,30})[<>] (.*\S)\s*$')


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #

@dataclass
class Config:
    logs_dir: str = ""
    target_lang: str = "ru"
    channels: list = field(default_factory=lambda: CHANNELS.copy())
    only_foreign: bool = True   # переводить только сообщения не на целевом языке
    on_top: bool = True         # окно поверх других
    show_original: bool = True  # показывать текст оригинала


def load_config() -> Config:
    cfg = Config()
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        except Exception:
            pass
    if not cfg.logs_dir:
        cfg.logs_dir = guess_logs_dir()
    return cfg


def save_config(cfg: Config) -> None:
    try:
        CONFIG_FILE.write_text(
            json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def guess_logs_dir() -> str:
    r"""Найти свежайшую папку логов: <Дом>\wurm\players\<Игрок>\logs."""
    players_root = Path.home() / "wurm" / "players"
    if players_root.is_dir():
        players = [p for p in players_root.iterdir() if p.is_dir()]
        if players:
            newest = max(players, key=lambda p: p.stat().st_mtime)
            logs = newest / "logs"
            if logs.is_dir():
                return str(logs)
    return ""


# --------------------------------------------------------------------------- #
# Разбор строк лога
# --------------------------------------------------------------------------- #

def parse_line(line: str):
    """Вернуть (время, имя, текст) или None, если строка не сообщение чата."""
    m = RE_CHAT.match(line)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = RE_PM.match(line)
    if m:
        return m.group(1), m.group(3), m.group(4)
    return None


def channel_of(filename: str) -> str:
    """_Local.2026-10.txt -> Local ; _PM.SomePlayer.txt -> PM."""
    stem = filename[1:] if filename.startswith("_") else filename
    return stem.split(".")[0]


# --------------------------------------------------------------------------- #
# Переводчик (Google Translate, client=gtx, без ключа)
# --------------------------------------------------------------------------- #

class GtxTranslator:
    URL = "https://translate.googleapis.com/translate_a/single"

    def __init__(self, target: str, interval: float = 0.15, max_len: int = 1800):
        self.target = target
        self.interval = interval          # пауза между запросами, чтобы не банили
        self.max_len = max_len            # gtx глючит на очень длинных строках
        self.cache: dict = {}
        self._last = 0.0
        self._lock = threading.Lock()

    def translate(self, text: str):
        """Вернуть (перевод, определённый_язык_источника). Кэширует результат."""
        text = text[: self.max_len]
        key = (text, self.target)
        with self._lock:
            if key in self.cache:
                return self.cache[key]
        result = self._request_with_retry(text)
        with self._lock:
            self.cache[key] = result
        return result

    def _request_with_retry(self, text: str):
        last_err = None
        for attempt in range(3):
            try:
                return self._request(text)
            except Exception as exc:        # noqa: BLE001
                last_err = exc
                time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"Translation failed: {last_err}")

    def _request(self, text: str):
        pause = self.interval - (time.monotonic() - self._last)
        if pause > 0:
            time.sleep(pause)
        params = {"client": "gtx", "sl": "auto", "tl": self.target, "dt": "t", "q": text}
        url = self.URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self._last = time.monotonic()
        translated = "".join(seg[0] for seg in data[0] if seg and seg[0])
        detected = data[2] if len(data) > 2 else "auto"
        return translated, detected


# --------------------------------------------------------------------------- #
# Поток-наблюдатель за логами
# --------------------------------------------------------------------------- #

class LogWatcher(threading.Thread):
    """Сканирует файлы логов и кладёт новые сообщения в out_queue.

    Каждый элемент: ("msg", channel, time, name, text).
    При первом обнаружении файл позиционируется в конец (старые сообщения
    не вываливаются пачкой).
    """

    def __init__(self, logs_dir: str, channels: list, out_queue: queue.Queue,
                 stop_event: threading.Event, poll: float = 0.5):
        super().__init__(daemon=True)
        self.logs_dir = Path(logs_dir)
        self.channels = set(channels)
        self.out = out_queue
        self.stop = stop_event
        self.poll = poll
        self.offsets: dict = {}

    def run(self):
        while not self.stop.is_set():
            try:
                self._scan_once()
            except Exception as exc:        # noqa: BLE001
                self.out.put(("error", str(exc)))
            self.stop.wait(self.poll)

    def _scan_once(self):
        if not self.logs_dir.is_dir():
            return
        for path in sorted(self.logs_dir.glob("_*.txt")):
            ch = channel_of(path.name)
            if ch not in self.channels:
                continue
            try:
                if path not in self.offsets:
                    # первый раз — только новые сообщения
                    self.offsets[path] = path.stat().st_size
                    continue
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(self.offsets[path])
                    new_data = fh.read()
                    self.offsets[path] = fh.tell()
            except OSError:
                continue
            for line in new_data.splitlines():
                parsed = parse_line(line)
                if parsed:
                    t, name, text = parsed
                    self.out.put(("msg", ch, t, name, text))


# --------------------------------------------------------------------------- #
# Поток-переводчик
# --------------------------------------------------------------------------- #

class TranslatorThread(threading.Thread):
    """Берёт сообщения из raw_queue, переводит, кладёт в done_queue.

    Каждый элемент: ("done", channel, time, name, text, translation, detected).
    Если перевод не нужен (only_foreign и язык совпал) — translation = None.
    """

    def __init__(self, translator: GtxTranslator, raw_queue: queue.Queue,
                 done_queue: queue.Queue, stop_event: threading.Event,
                 target_lang: str, only_foreign: bool):
        super().__init__(daemon=True)
        self.tr = translator
        self.raw = raw_queue
        self.done = done_queue
        self.stop = stop_event
        self.target_lang = target_lang
        self.only_foreign = only_foreign

    def run(self):
        while not self.stop.is_set():
            try:
                item = self.raw.get(timeout=0.2)
            except queue.Empty:
                continue
            if not item or item[0] != "msg":
                continue
            _, ch, t, name, text = item
            translation = detected = None
            try:
                if self._needs_translation(text):
                    translation, detected = self.tr.translate(text)
            except Exception as exc:        # noqa: BLE001
                translation, detected = None, f"error: {exc}"
            self.done.put(("done", ch, t, name, text, translation, detected))

    def _needs_translation(self, text: str) -> bool:
        if not self.only_foreign:
            return True
        # Быстрый предварительный фильтр кириллицы/латиницы, чтобы лишний раз
        # не дергать сеть на явно своих сообщениях.
        if self.target_lang == "ru" and re.search(r"[А-Яа-яЁё]", text):
            return False
        if self.target_lang == "en" and re.search(r"[A-Za-z]", text) \
                and not re.search(r"[А-Яа-яЁё]", text):
            return False
        return True


# --------------------------------------------------------------------------- #
# Оконный интерфейс (tkinter)
# --------------------------------------------------------------------------- #

def run_gui(cfg: Config) -> None:
    import tkinter as tk
    from tkinter import filedialog, scrolledtext, ttk

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("720x520")
    root.minsize(520, 360)

    app_state = {
        "stop": threading.Event(),
        "raw": queue.Queue(),
        "done": queue.Queue(),
        "watcher": None,
        "worker": None,
        "running": False,
    }

    # --- верхняя панель: папка логов + язык ---
    top = ttk.Frame(root)
    top.pack(fill="x", padx=8, pady=(8, 2))
    ttk.Label(top, text="Логи:").pack(side="left")
    dir_var = tk.StringVar(value=cfg.logs_dir)
    ttk.Entry(top, textvariable=dir_var, width=55).pack(side="left", padx=4)
    ttk.Button(top, text="…", width=3,
               command=lambda: dir_var.set(
                   filedialog.askdirectory() or dir_var.get())).pack(side="left")

    top2 = ttk.Frame(root)
    top2.pack(fill="x", padx=8, pady=2)
    ttk.Label(top2, text="Переводить на:").pack(side="left")
    lang_var = tk.StringVar(value=LANGUAGES.get(cfg.target_lang, cfg.target_lang))
    lang_menu = ttk.OptionMenu(
        top2, lang_var, lang_var.get(), *LANGUAGES.values())
    lang_menu.pack(side="left", padx=4)
    lang_by_label = {v: k for k, v in LANGUAGES.items()}

    only_var = tk.BooleanVar(value=cfg.only_foreign)
    ttk.Checkbutton(top2, text="Только иностранные", variable=only_var).pack(side="left", padx=8)
    orig_var = tk.BooleanVar(value=cfg.show_original)
    ttk.Checkbutton(top2, text="Показывать оригинал", variable=orig_var).pack(side="left")
    top_var = tk.BooleanVar(value=cfg.on_top)
    top_chk = ttk.Checkbutton(top2, text="Поверх окон", variable=top_var)
    top_chk.pack(side="left", padx=8)
    root.attributes("-topmost", cfg.on_top)
    top_chk.config(command=lambda: root.attributes("-topmost", top_var.get()))

    # --- панель каналов ---
    ch_frame = ttk.LabelFrame(root, text="Каналы")
    ch_frame.pack(fill="x", padx=8, pady=4)
    ch_vars = {}
    for name in CHANNELS:
        v = tk.BooleanVar(value=(name in cfg.channels))
        ch_vars[name] = v
        ttk.Checkbutton(ch_frame, text=name, variable=v).pack(side="left", padx=6, pady=3)

    # --- текстовое поле ---
    body = ttk.Frame(root)
    body.pack(fill="both", expand=True, padx=8, pady=4)
    txt = scrolledtext.ScrolledText(body, wrap="word", state="disabled",
                                    font=("Segoe UI", 10))
    txt.pack(fill="both", expand=True)
    txt.tag_config("hdr", foreground="#005a9e", font=("Segoe UI", 9, "bold"))
    txt.tag_config("orig", foreground="#777777")
    txt.tag_config("err", foreground="#cc0000")

    # --- нижняя панель ---
    bottom = ttk.Frame(root)
    bottom.pack(fill="x", padx=8, pady=(2, 8))
    status_var = tk.StringVar(value="Остановлено")
    ttk.Label(bottom, textvariable=status_var).pack(side="left")
    run_btn = ttk.Button(bottom, text="▶ Старт")
    run_btn.pack(side="right", padx=4)
    clear_btn = ttk.Button(bottom, text="Очистить",
                           command=lambda: (txt.config(state="normal"),
                                            txt.delete("1.0", "end"),
                                            txt.config(state="disabled")))
    clear_btn.pack(side="right")

    def append(lines, tags):
        txt.config(state="normal")
        for line, tag in zip(lines, tags):
            txt.insert("end", line, tag or ())
        txt.see("end")
        txt.config(state="disabled")

    def collect_cfg():
        cfg.logs_dir = dir_var.get().strip()
        cfg.target_lang = lang_by_label.get(lang_var.get(), "ru")
        cfg.channels = [n for n, v in ch_vars.items() if v.get()]
        cfg.only_foreign = only_var.get()
        cfg.show_original = orig_var.get()
        cfg.on_top = top_var.get()
        save_config(cfg)

    def start():
        collect_cfg()
        if not cfg.logs_dir or not Path(cfg.logs_dir).is_dir():
            status_var.set("Укажите существующую папку логов (…\\wurm\\players\\<Игрок>\\logs)")
            return
        app_state["stop"].clear()
        app_state["raw"] = queue.Queue()
        app_state["done"] = queue.Queue()
        tr = GtxTranslator(cfg.target_lang)
        app_state["watcher"] = LogWatcher(
            cfg.logs_dir, cfg.channels, app_state["raw"], app_state["stop"])
        app_state["worker"] = TranslatorThread(
            tr, app_state["raw"], app_state["done"], app_state["stop"],
            cfg.target_lang, cfg.only_foreign)
        app_state["watcher"].start()
        app_state["worker"].start()
        app_state["running"] = True
        run_btn.config(text="■ Стоп")
        status_var.set(f"Слежу за {len(cfg.channels)} каналами → {cfg.logs_dir}")

    def stop():
        app_state["stop"].set()
        app_state["running"] = False
        run_btn.config(text="▶ Старт")
        status_var.set("Остановлено")

    def toggle():
        stop() if app_state["running"] else start()
    run_btn.config(command=toggle)

    def poll():
        try:
            while True:
                item = app_state["done"].get_nowait()
                if item[0] != "done":
                    continue
                _, ch, t, name, text, trans, detected = item
                hdr = f"[{t}] [{ch}] {name}:\n"
                lines, tags = [hdr], ["hdr"]
                if trans is None:
                    lines.append(text + "\n\n")
                    tags.append(None)
                else:
                    if orig_var.get() and trans != text:
                        lines.append(text + "\n")
                        tags.append("orig")
                    if isinstance(detected, str) and detected.startswith("error"):
                        lines.append(f"(ошибка перевода: {detected})\n\n")
                        tags.append("err")
                    else:
                        lines.append(trans + "\n\n")
                        tags.append(None)
                append(lines, tags)
        except queue.Empty:
            pass
        root.after(100, poll)

    def on_close():
        stop()
        collect_cfg()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(100, poll)
    root.mainloop()


# --------------------------------------------------------------------------- #
# Консольный режим
# --------------------------------------------------------------------------- #

def run_console(cfg: Config) -> None:
    stop = threading.Event()
    raw: queue.Queue = queue.Queue()
    done: queue.Queue = queue.Queue()
    tr = GtxTranslator(cfg.target_lang)
    watcher = LogWatcher(cfg.logs_dir, cfg.channels, raw, stop)
    worker = TranslatorThread(tr, raw, done, stop, cfg.target_lang, cfg.only_foreign)
    watcher.start()
    worker.start()
    print(f"Слежу за {cfg.logs_dir} (каналы: {', '.join(cfg.channels)}). Ctrl+C — выход.")
    try:
        while True:
            try:
                item = done.get(timeout=0.5)
            except queue.Empty:
                continue
            if item[0] != "done":
                continue
            _, ch, t, name, text, trans, detected = item
            if trans is None or trans == text:
                print(f"[{t}] [{ch}] {name}: {text}")
            else:
                print(f"[{t}] [{ch}] {name}: {text}\n    → {trans}")
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


# --------------------------------------------------------------------------- #
# Самопроверка
# --------------------------------------------------------------------------- #

def self_test() -> int:
    ok = True

    # 1. Разбор строк
    cases = [
        ("[21:42:03] PlayerOne: hello there", ("21:42:03", "PlayerOne", "hello there")),
        ("[09:05:11] You create a stone chisel.", None),
        ('["09:05:12"] <PlayerTwo> how are you', ("09:05:12", "PlayerTwo", "how are you")),
        ('["09:05:13"] >PlayerTwo< fine thanks', ("09:05:13", "PlayerTwo", "fine thanks")),
    ]
    for line, expected in cases:
        got = parse_line(line)
        status = "OK " if got == expected else "FAIL"
        if got != expected:
            ok = False
        print(f"  [{status}] parse: {line!r} -> {got}")

    # 2. Определение канала по имени файла
    for fname, ch in [("_Local.2026-10.txt", "Local"),
                      ("_PM.SomePlayer.txt", "PM"),
                      ("_Kingdom.2026-10.txt", "Kingdom")]:
        got = channel_of(fname)
        status = "OK " if got == ch else "FAIL"
        if got != ch:
            ok = False
        print(f"  [{status}] channel: {fname} -> {got}")

    # 3. Перевод (нужна сеть)
    try:
        tr = GtxTranslator("ru")
        trans, detected = tr.translate("hello world")
        status = "OK " if detected == "en" and "привет" in trans.lower() else "FAIL"
        print(f"  [{status}] translate: 'hello world' -> {trans!r} (detected={detected})")
        if status == "FAIL":
            ok = False
    except Exception as exc:      # noqa: BLE001
        ok = False
        print(f"  [FAIL] translate: {exc}")

    print("Итог:", "все проверки пройдены" if ok else "есть ошибки")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--console", action="store_true", help="консольный режим")
    parser.add_argument("--test", action="store_true", help="самопроверка и выход")
    parser.add_argument("--logs", help="папка логов (…\\wurm\\players\\<Игрок>\\logs)")
    parser.add_argument("--lang", help="целевой язык, напр. ru/en/de")
    args = parser.parse_args()

    if args.test:
        return self_test()

    cfg = load_config()
    if args.logs:
        cfg.logs_dir = args.logs
    if args.lang:
        cfg.target_lang = args.lang

    if args.console:
        if not cfg.logs_dir:
            print("Не найдена папка логов. Укажите: --logs C:\\...\\wurm\\players\\<Игрок>\\logs")
            return 1
        run_console(cfg)
        return 0

    run_gui(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
