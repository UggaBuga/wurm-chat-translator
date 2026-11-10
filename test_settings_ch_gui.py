# -*- coding: utf-8 -*-
"""Дымовая проверка зеркала «Звук и уведомления по каналам» в «Настройках»
и пассивности значков на вкладках (вариант А UX-правки):
1. значки ♪/✉ на вкладках больше не кликабельны (нет hand2);
2. в «Настройках» есть сетка: каждый канал — две галочки (звук, уведомления);
3. галочка сохраняет флаг в cfg и обновляет значок вкладки;
4. подсказки значков предлагают переключать правой кнопкой.
Гоняет реальный GUI; окно мелькает на экране.

Особенность: «Настройки» модальные (wait_window) — шаги по диалогу
планируются через root.after ДО вызова invoke() кнопки (паттерн
test_dig_gui.py)."""
import os
import sys
import json
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wurm_chat_gui as m
import tkinter as tk
from tkinter import ttk

tmp = tempfile.mkdtemp(prefix="wurm_smoke_set_")
logs = os.path.join(tmp, "players", "TestChar", "logs")
os.makedirs(logs)
m.CONFIG_FILE = os.path.join(tmp, "cfg.json")
m.DEBUG_FILE = os.path.join(tmp, "dbg.log")
json.dump({"clients": [{"name": "TestChar", "path": logs}]},
          open(m.CONFIG_FILE, "w", encoding="utf-8"))

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


def find_settings(root):
    for w in walk(root):
        if isinstance(w, tk.Toplevel) and w.title() == m.tr("settings_title"):
            return w
    return None


def find_button(win, text):
    for w in walk(win):
        if isinstance(w, ttk.Button) and w.cget("text") == text:
            return w
    return None


def tab_icons(root):
    """Значки вкладок: tk.Label с текстом ♪ или ✉."""
    return [w for w in walk(root)
            if isinstance(w, tk.Label) and w.cget("text") in ("♪", "✉")]


def cfg_now():
    return json.load(open(m.CONFIG_FILE, encoding="utf-8"))


ROOT = {"r": None}


def step_passive_icons(root):
    ROOT["r"] = root
    icons = tab_icons(root)
    check("значки ♪/✉ найдены на вкладках", len(icons) >= 2 * len(m.CHANNELS))
    bad_cursor = [i for i in icons if str(i.cget("cursor")) == "hand2"]
    check("значки не кликабельны (нет hand2)", not bad_cursor)
    tt = m.tr("tab_tt_sound_on")
    check("подсказка предлагает правую кнопку",
          any(w in tt for w in ("ПКМ", "правая", "правой", "right-click",
                                "Rechtsklick", "clic derecho",
                                "botão direito", "clic droit", "右键")))


def step_open_settings(root):
    b = find_button(root, m.tr("btn_settings"))
    check("кнопка «Настройки…» найдена", b is not None)
    if b is None:
        return
    root.after(80, interact_grid)
    root.after(5000, interact_timeout)
    b.invoke()          # блокируется в wait_window — interact сработает
    root.update()
    check("окно настроек закрылось", find_settings(root) is None)


TIMEOUT = {"fired": False}


def interact_timeout():
    TIMEOUT["fired"] = True
    win = find_settings(ROOT["r"])
    if win is not None:
        win.destroy()


def cell_of(win, channel):
    for w in walk(win):
        if isinstance(w, ttk.Label) and w.cget("text") == channel:
            return w.master
    return None


def cb_in(cell, prefix):
    if cell is None:
        return None
    return next((c for c in cell.winfo_children()
                 if isinstance(c, ttk.Checkbutton)
                 and str(c.cget("text")).startswith(prefix)), None)


def interact_grid():
    root = ROOT["r"]
    win = find_settings(root)
    check("окно настроек открылось", win is not None)
    if win is None:
        return
    win.update_idletasks()
    win.update()
    cbs = [w for w in walk(win)
           if isinstance(w, ttk.Checkbutton)
           and str(w.cget("text")).startswith(("🌐 ", "♪ ", "✉ "))]
    tr = [w for w in cbs if str(w.cget("text")).startswith("🌐 ")]
    snd = [w for w in cbs if str(w.cget("text")).startswith("♪ ")]
    ntf = [w for w in cbs if str(w.cget("text")).startswith("✉ ")]
    check("галочек «перевод» по числу каналов", len(tr) == len(m.CHANNELS))
    check("галочек «звук» по числу каналов", len(snd) == len(m.CHANNELS))
    check("галочек «уведомления» по числу каналов", len(ntf) == len(m.CHANNELS))
    check("перевод по умолчанию включён в обычных каналах",
          sum(1 for w in tr if w.instate(["selected"])) == len(tr) - 1)
    # национальные чаты («Russian GL») по умолчанию БЕЗ перевода
    gl_cell = cell_of(win, "Russian GL")
    gl_cb = cb_in(gl_cell, "🌐 ")
    check("у Russian GL перевод выключен по умолчанию",
          gl_cb is not None and not gl_cb.instate(["selected"]))
    cap = [w for w in walk(win) if isinstance(w, ttk.Label)
           and w.cget("text") == m.tr("opt_channels")]
    check("заголовок секции каналов есть", len(cap) == 1)
    hint = [w for w in walk(win) if isinstance(w, ttk.Label)
            and w.cget("text") == m.tr("opt_channels_hint")]
    check("сноска про правую кнопку есть", len(hint) == 1)

    cell = cell_of(win, "Trade")
    check("ячейка канала Trade найдена", cell is not None)
    cb = cb_in(cell, "♪ ")
    check("галочка звука в ячейке найдена", cb is not None)
    if cb is not None:
        check("по умолчанию звук выключен",
              not cfg_now().get("sound_channels"))
        cb.invoke()
        win.update()
        check("флаг sound_channels=[Trade] сохранён",
              cfg_now().get("sound_channels") == ["Trade"])
        on = [i for i in tab_icons(root)
              if i.cget("text") == "♪" and str(i.cget("fg")) == "#222222"]
        check("значок ♪ Trade на вкладке потемнел", len(on) >= 1)

    cell = cell_of(win, "Event")
    check("ячейка канала Event найдена", cell is not None)
    cb = cb_in(cell, "✉ ")
    check("галочка уведомлений в ячейке найдена", cb is not None)
    if cb is not None:
        cb.invoke()
        win.update()
        check("флаг notify_channels=[Event] сохранён",
              cfg_now().get("notify_channels") == ["Event"])
        on = [i for i in tab_icons(root)
              if i.cget("text") == "✉" and str(i.cget("fg")) == "#222222"]
        check("значок ✉ Event на вкладке потемнел", len(on) >= 1)

    # флаги не спутались между собой
    check("звук включён только у Trade",
          cfg_now().get("sound_channels") == ["Trade"])
    check("уведомления включены только у Event",
          cfg_now().get("notify_channels") == ["Event"])

    # выключение перевода канала: галочка 🌐 = «переводить», снимаем у Event
    cell = cell_of(win, "Event")
    cb = cb_in(cell, "🌐 ")
    check("галочка перевода в ячейке найдена", cb is not None)
    if cb is not None:
        check("перевод Event по умолчанию включён", bool(cb.instate(["selected"])))
        cb.invoke()
        win.update()
        check("Event в списке без перевода",
              "Event" in (cfg_now().get("no_translate_channels") or []))
        check("другие каналы переводятся",
              "Trade" not in (cfg_now().get("no_translate_channels") or []))

    b = find_button(win, m.tr("settings_close"))
    check("кнопка закрытия найдена", b is not None)
    if b is not None:
        b.invoke()


def step_final(root):
    check("таймаут не срабатывал", not TIMEOUT["fired"])


script = [step_passive_icons, step_open_settings, step_final]
m.run_gui()

bad = [n for n, ok in results if not ok]
print(f"\nВыполнено проверок: {len(results)}")
if bad:
    print("ПРОВАЛЕНО:")
    for n in bad:
        print("  -", n)
    sys.exit(1)
print("Итог: все проверки пройдены")
