# -*- coding: utf-8 -*-
"""Дымовая проверка редактирования пути клиента: в окне «Клиенты — папки
логов» выбранная строка подгружается в поля, правка пути + «Применить»
обновляет строку, «Закрыть» сохраняет новый путь в конфиг.
Гоняет реальный GUI; окно мелькает на экране."""
import os
import sys
import json
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wurm_chat_gui as m
import tkinter as tk
from tkinter import ttk

tmp = tempfile.mkdtemp(prefix="wurm_smoke_cli_")
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


def find_dialog(root):
    for w in walk(root):
        if isinstance(w, tk.Toplevel) and w.title() == m.tr("dlg_clients_title"):
            return w
    return None


def find_button(win, text):
    for w in walk(win):
        if isinstance(w, ttk.Button) and w.cget("text") == text:
            return w
    return None


NEW_PATH = os.path.join(tmp, "players", "TestChar2", "logs")


def step_open(root):
    b = find_button(root, m.tr("clients_btn"))
    check("кнопка «Клиенты…» найдена", b is not None)
    if b is None:
        return
    b.invoke()
    win = find_dialog(root)
    check("диалог клиентов открылся", win is not None)
    if win is not None:
        win.update_idletasks()
        win.update()
    root._cli_win = win


def step_select_and_edit(root):
    win = root._cli_win
    tree = next((w for w in walk(win) if isinstance(w, ttk.Treeview)), None)
    check("таблица найдена", tree is not None)
    if tree is None:
        return
    iid = tree.get_children()[0]
    tree.selection_set(iid)
    win.update()
    entries = [w for w in walk(win) if isinstance(w, ttk.Entry)]
    check("полей ввода два (имя и путь)", len(entries) == 2)
    if len(entries) != 2:
        return
    path_entry = entries[1]
    check("в поле пути текущий путь",
          path_entry.get() == logs)
    path_entry.delete(0, "end")
    path_entry.insert(0, NEW_PATH)
    apply = find_button(win, m.tr("btn_apply"))
    check("кнопка «Применить» найдена", apply is not None)
    if apply is not None:
        apply.invoke()
    win.update()
    vals = tree.item(iid, "values")
    check("строка таблицы обновилась", vals[1] == NEW_PATH)


# папка с логами для выборщика: внутри — пара файлов логов
PICK_DIR = os.path.join(tmp, "players", "Picker")
os.makedirs(PICK_DIR)
open(os.path.join(PICK_DIR, "_Local.2026-11.txt"), "w").write("x")
open(os.path.join(PICK_DIR, "_Event.2026-11.txt"), "w").write("x")


def step_picker(root):
    """Свой выборщик папок: открывается, видит логи в папке, выбор
    возвращает путь."""
    root_ref["w"] = root
    win = root._cli_win
    # свежая строка, чтобы выборщик открылся в стартовой папке
    tree = next(w for w in walk(win) if isinstance(w, ttk.Treeview))
    entries = [w for w in walk(win) if isinstance(w, ttk.Entry)]
    entries[1].delete(0, "end")
    entries[1].insert(0, PICK_DIR)
    browse = find_button(win, m.tr("btn_browse"))
    check("кнопка «Обзор…» найдена", browse is not None)
    if browse is None:
        return
    root.after(80, interact_picker)
    root.after(5000, picker_timeout)
    browse.invoke()     # блокируется в wait_window выборщика


def step_picker_done(root):
    win = root._cli_win
    entries = [w for w in walk(win) if isinstance(w, ttk.Entry)]
    check("выборщик вернул путь в поле", entries[1].get() == PICK_DIR)


TIMEOUT2 = {"fired": False}


def picker_timeout():
    TIMEOUT2["fired"] = True
    for w in walk(root_ref["w"]):
        if isinstance(w, tk.Toplevel) and w.title() == m.tr("pick_title"):
            w.destroy()


root_ref = {"w": None}


def interact_picker():
    dlg = None
    for w in walk(root_ref["w"]):
        if isinstance(w, tk.Toplevel) and w.title() == m.tr("pick_title"):
            dlg = w
            break
    check("выборщик открылся", dlg is not None)
    if dlg is None:
        return
    dlg.update_idletasks()
    dlg.update()
    labels = [w for w in walk(dlg) if isinstance(w, ttk.Label)]
    all_text = "\n".join(str(l.cget("text")) for l in labels)
    check("статус показывает число логов", "2" in all_text
          and "_Local.2026-11.txt" in all_text)
    green = [l for l in labels
             if str(l.cget("foreground")) in ("#1a7a1a", "green")]
    check("статус зелёный при найденных логах", len(green) >= 1)
    # «Вверх» + возврат: в родителе логов нет — статус серый/красный
    up = find_button(dlg, m.tr("pick_up"))
    if up is not None:
        up.invoke()
        dlg.update()
        labels2 = [w for w in walk(dlg) if isinstance(w, ttk.Label)]
        texts2 = "\n".join(str(l.cget("text")) for l in labels2)
        check("в папке без логов статус не зелёный",
              "_Local.2026-11.txt" not in texts2)
        # вернёмся: ввести путь и нажать Enter
        ent = next(w for w in walk(dlg) if isinstance(w, ttk.Entry))
        ent.delete(0, "end")
        ent.insert(0, PICK_DIR)
        ent.event_generate("<Return>")
        dlg.update()
    ok = find_button(dlg, m.tr("pick_choose"))
    check("кнопка «Выбрать эту папку» найдена", ok is not None)
    if ok is not None:
        ok.invoke()


def step_close_and_verify(root):
    win = root._cli_win
    b = find_button(win, m.tr("btn_close"))
    check("кнопка «Закрыть» найдена", b is not None)
    if b is not None:
        b.invoke()
    root.update()
    check("диалог закрылся", find_dialog(root) is None)
    cfg = json.load(open(m.CONFIG_FILE, encoding="utf-8"))
    paths = [c.get("path") for c in cfg.get("clients", [])]
    check("новый путь сохранён в конфиг", NEW_PATH in paths)
    check("старый путь заменён, не дублируется",
          paths.count(logs) == 0)


script = [step_open, step_select_and_edit, step_picker, step_picker_done,
          step_close_and_verify]
m.run_gui()

bad = [n for n, ok in results if not ok]
print(f"\nВыполнено проверок: {len(results)}")
if bad:
    print("ПРОВАЛЕНО:")
    for n in bad:
        print("  -", n)
    sys.exit(1)
print("Итог: все проверки пройдены")
