# -*- coding: utf-8 -*-
"""Дымовая проверка диалога «Сигналы»: открытие, выпадающие списки канала
и звука, сохранение выбора в конфиг. Гоняет реальный GUI через поддельный
mainloop; окно мелькает на экране."""
import os
import sys
import json
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wurm_chat_gui as m
import tkinter as tk
from tkinter import ttk

tmp = tempfile.mkdtemp(prefix="wurm_smoke_")
logs = os.path.join(tmp, "players", "TestChar", "logs")
os.makedirs(logs)
m.CONFIG_FILE = os.path.join(tmp, "cfg.json")
m.DEBUG_FILE = os.path.join(tmp, "dbg.log")
m.DEFAULTS = dict(m.DEFAULTS, clients=[{"name": "TestChar", "path": logs}])

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("  [OK ] " if cond else "  [FAIL] ") + name)


script = []


def fake_mainloop(self):
    """Гоняет шаги сценария и одновременно качает очередь Tk-событий,
    иначе никто не обработает root.after(...) и шаги не выполнятся."""
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
        self.after(120, step)
    self.after(120, step)
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


def find_button(root, text):
    for w in walk(root):
        if isinstance(w, ttk.Button) and w.cget("text") == text:
            return w
    return None


def find_dialog(root):
    for w in walk(root):
        if isinstance(w, tk.Toplevel) and w.title().startswith("Сигналы"):
            return w
    return None


def find_listbox(root):
    for w in walk(root):
        if isinstance(w, tk.Listbox):
            return w
    return None


def find_tree(win):
    for w in walk(win):
        if isinstance(w, ttk.Treeview):
            return w
    return None


# Событиям от event_generate нужно явное нарастающее время: без него Tk
# склеивает щелчки в «двойной» (на дереве есть привязка Double-Button-1)
# и одиночные нажатия молча пропадают.
_ev_time = [10 ** 6]


def gen_click(tree, win, col, iid="0"):
    _ev_time[0] += 2000
    bbox = tree.bbox(iid, col)
    x, y, w, h = bbox
    tree.event_generate("<Button-1>", x=x + w // 2, y=y + h // 2,
                        rootx=win.winfo_rootx() + 100,
                        rooty=win.winfo_rooty() + 100,
                        time=_ev_time[0])
    win.update()


# --- шаги сценария ---
def step_open_dialog(root):
    b = find_button(root, "Сигналы…")
    check("кнопка «Сигналы…» найдена", b is not None)
    b.invoke()
    win = find_dialog(root)
    check("диалог открылся", win is not None)
    if win is not None:
        win.update_idletasks()
        win.update()
    root._alerts_win = win


def step_click_sound_cell(root):
    win = root._alerts_win
    tree = find_tree(win)
    bbox = tree.bbox("0", "sound")
    check("ячейка «Звук» видима", bool(bbox))
    if bbox:
        gen_click(tree, win, "sound")


def step_pick_sound(root):
    win = root._alerts_win
    lb = find_listbox(win)
    check("список звуков раскрылся", lb is not None)
    if lb is None:
        return
    items = lb.get(0, "end")
    check("пункты: Выкл./Как у канала/6 звуков", len(items) == 8)
    # выбираем «Колокол» (индекс 3: 0 Выкл, 1 канал, 2 Колокольчик, 3 Колокол)
    idx = next(i for i, s in enumerate(items) if "Колокол" in s and "Колоколь" not in s)
    lb._on_pick(idx)
    win.update()
    cfg = json.load(open(m.CONFIG_FILE, encoding="utf-8"))
    rule = cfg["alerts"][0]
    check("sound_id=kолокол сохранён", rule.get("sound_id") == "bell")
    check("звук включён", bool(rule.get("sound")))
    lb2 = find_listbox(win)
    check("список закрылся после выбора", lb2 is None)
    tree = find_tree(win)
    check("ячейка показывает «Колокол»",
          tree.item("0", "values")[3] == "Колокол")


def step_pick_off(root):
    win = root._alerts_win
    tree = find_tree(win)
    gen_click(tree, win, "sound")
    lb = find_listbox(win)
    check("список звуков раскрылся снова", lb is not None)
    if lb is None:
        return
    lb._on_pick(0)          # «Выкл.»
    win.update()
    cfg = json.load(open(m.CONFIG_FILE, encoding="utf-8"))
    check("звук выключен", not cfg["alerts"][0].get("sound"))
    check("ячейка показывает «—»", find_tree(win).item("0", "values")[3] == "—")


def step_channel_dropdown(root):
    win = root._alerts_win
    tree = find_tree(win)
    gen_click(tree, win, "channel")
    lb = find_listbox(win)
    check("чек-лист каналов раскрылся", lb is not None)
    if lb is None:
        return
    items = lb.get(0, "end")
    check("пункты: Любой + каналы", len(items) == 1 + len(m.CHANNELS))
    lb._on_pick(0)          # «Любой» — сброс каналов
    win.update()
    lb2 = find_listbox(win)
    check("чек-лист остался открыт (мультивыбор)",
          lb2 is not None and lb2.get(0, "end")[0].startswith("☑"))
    lb2._on_pick(next(i for i, s in enumerate(lb2.get(0, "end"))
                      if "Trade" in s))
    win.update()
    cfg = json.load(open(m.CONFIG_FILE, encoding="utf-8"))
    check("канал Trade добавлен",
          "Trade" in (cfg["alerts"][0].get("channels") or []))
    # закрыть список кликом мимо
    win.event_generate("<Button-1>", x=5, y=5,
                       rootx=win.winfo_rootx() + 5,
                       rooty=win.winfo_rooty() + 5)
    win.update()
    check("список закрылся по клику мимо", find_listbox(win) is None)


def step_close(root):
    win = root._alerts_win
    for w in walk(win):
        if isinstance(w, ttk.Button) and w.cget("text") == "Закрыть":
            w.invoke()
            break
    root.update()
    check("диалог закрылся", find_dialog(root) is None)


script = [step_open_dialog, step_click_sound_cell, step_pick_sound,
          step_pick_off, step_channel_dropdown, step_close]
m.run_gui()

failed = [n for n, ok in results if not ok]
if not results:
    print("НИ ОДНОЙ проверки не выполнено — сценарий не отработал")
    sys.exit(1)
print(f"Выполнено проверок: {len(results)}")
print("Итог:", "все проверки пройдены" if not failed else f"ошибки: {failed}")
sys.exit(1 if failed else 0)
