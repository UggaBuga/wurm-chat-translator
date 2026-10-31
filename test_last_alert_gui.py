# -*- coding: utf-8 -*-
"""Дымовая проверка окна последнего оповещения: строка с подстрокой сигнала
появляется в логе Event -> всплывает окно с текстом сигнала -> кнопка
«К сообщению» закрывает окно и переключает вкладку на Event. Гоняет реальный
GUI и настоящий наблюдатель логов; окно мелькает на экране."""
import os
import sys
import json
import time
import datetime
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wurm_chat_gui as m
import tkinter as tk
from tkinter import ttk

tmp = tempfile.mkdtemp(prefix="wurm_smoke_la_")
logs = os.path.join(tmp, "players", "TestChar", "logs")
os.makedirs(logs)
m.CONFIG_FILE = os.path.join(tmp, "cfg.json")
m.DEBUG_FILE = os.path.join(tmp, "dbg.log")
# Сигналы превыше значков каналов: даже при полностью выключенных
# звуках/уведомлениях каналов сигнал обязан прорваться (звук + окно оповещения)
json.dump({
    "clients": [{"name": "TestChar", "path": logs}],
}, open(m.CONFIG_FILE, "w", encoding="utf-8"))
# notify=False — без реальных системных тостов на машине во время теста
m.DEFAULTS["alerts"] = [{
    "channels": ["Event"], "pattern": "complete the location details",
    "note": "обнаружен центр поселения с сундучком, иди ищи и копай",
    "enabled": True, "sound": True, "notify": False}]

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("  [OK ] " if cond else "  [FAIL] ") + name)


script = []


def fake_mainloop(self):
    def step():
        try:
            fn = script.pop(0)
        except IndexError:
            self.destroy()
            return
        try:
            fn(self)
        except Exception:
            traceback.print_exc()
            self.destroy()
            return
        self.after(200, step)
    self.after(200, step)
    import time as _time
    while script and self.winfo_exists():
        try:
            self.update()
        except tk.TclError:
            break
        _time.sleep(0.02)


tk.Tk.mainloop = fake_mainloop


def walk(w):
    yield w
    for c in w.winfo_children():
        yield from walk(c)


def find_lastalert(root):
    for w in walk(root):
        if isinstance(w, tk.Toplevel) \
                and w.title().startswith(tr_title_prefix()):
            return w
    return None


def tr_title_prefix():
    return m.tr("lastalert_title")[:6]


def find_button(win, text):
    for w in walk(win):
        if isinstance(w, ttk.Button) and w.cget("text") == text:
            return w
    return None


NOTE_TEXT = "обнаружен центр поселения с сундучком, иди ищи и копай"


def event_log_path():
    now = datetime.datetime.now()
    return os.path.join(logs, f"_Event.{now.year}-{now.month:02d}.txt")


def step_write_log(root):
    # файл появляется уже после старта наблюдателя — будет прочтён целиком
    with open(event_log_path(), "a", encoding="utf-8") as f:
        f.write("[12:34:56] You carefully examine the marker and complete the "
                "location details of the old settlement.\n")


TRIES = {"n": 40}


def step_wait_popup(root):
    win = find_lastalert(root)
    if win is None and TRIES["n"] > 0:
        TRIES["n"] -= 1
        if os.path.exists(event_log_path()):    # подкладываем ещё раз на всякий
            step_write_log(root)
        script.insert(0, step_wait_popup)       # перепроверим на следующем круге
        return
    check("окно оповещения появилось", win is not None)
    if win is None:
        return
    win.update_idletasks()
    labels = [w for w in walk(win) if isinstance(w, ttk.Label)]
    all_text = "\n".join(l.cget("text") for l in labels)
    import re as _re
    check("заголовок: время и канал Event",
          bool(_re.search(r"\d{2}:\d{2}:\d{2}", all_text))
          and "Event" in all_text)
    check("текст уведомления из правила", NOTE_TEXT in all_text)
    root._la_win = win


def step_jump(root):
    win = getattr(root, "_la_win", None)
    b = find_button(win, m.tr("lastalert_jump")) if win is not None else None
    check("кнопка «К сообщению» найдена", b is not None)
    if b is None:
        return
    b.invoke()
    root.update()
    check("окно закрылось после перехода", find_lastalert(root) is None)


script = [step_write_log, step_write_log, step_wait_popup, step_jump]
m.run_gui()

failed = [n for n, ok in results if not ok]
if not results:
    print("НИ ОДНОЙ проверки не выполнено — сценарий не отработал")
    sys.exit(1)
print(f"Выполнено проверок: {len(results)}")
print("Итог:", "все проверки пройдены" if not failed else f"ошибки: {failed}")
sys.exit(1 if failed else 0)
