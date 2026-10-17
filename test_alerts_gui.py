# -*- coding: utf-8 -*-
"""GUI-тест диалога «Сигналы на сообщения»: исполняет РЕАЛЬНЫЙ код функции
open_alerts_dialog, извлечённый из wurm_chat_gui.py через ast, и симулирует
мышь/клавиатуру. Проверяет: переключение флажка, смену канала, правку
подстроки двойным щелчком, добавление и удаление правила, автосохранение."""
import ast
import importlib.util
import sys
import tkinter as tk
from tkinter import ttk

BASE = r"E:\KimiData\kimi\Workspaces\1с тест умений\wurm_chat_translator"
SRC_PATH = BASE + r"\wurm_chat_gui.py"

SRC = open(SRC_PATH, encoding="utf-8").read()
mod = ast.parse(SRC)
fn = next(n for n in ast.walk(mod)
          if isinstance(n, ast.FunctionDef) and n.name == "open_alerts_dialog")
code = ast.get_source_segment(SRC, fn)

spec = importlib.util.spec_from_file_location("wct", SRC_PATH)
m = importlib.util.module_from_spec(spec)
sys.modules["wct"] = m
spec.loader.exec_module(m)

cfg = {"alerts": [{"channel": "Event",
                   "pattern": "complete the location details",
                   "enabled": True, "sound": True, "notify": True}]}
saved = []


def save_config(c):
    saved.append([dict(r) for r in c.get("alerts", [])])


root = tk.Tk()
root.geometry("120x60+0+0")
ns = {"tk": tk, "ttk": ttk, "tr": m.tr, "cfg": cfg,
      "save_config": save_config, "CHANNELS": m.CHANNELS, "root": root}
exec(code, ns)
ns["open_alerts_dialog"]()
win = root.winfo_children()[-1]
win.deiconify()
for _ in range(5):
    win.update()
    win.update_idletasks()


def find_tree(w):
    for ch in w.winfo_children():
        if isinstance(ch, ttk.Treeview):
            return ch
        r = find_tree(ch)
        if r is not None:
            return r


tree = find_tree(win)


def click(col):
    bbox = tree.bbox("0", col)
    print("bbox", col, bbox, flush=True)
    assert bbox, f"нет bbox для {col}"
    x, y, w, h = bbox
    tree.event_generate("<Button-1>", x=x + w // 2, y=y + h // 2)
    win.update()


def dblclick(col):
    bbox = tree.bbox("0", col)
    assert bbox, f"нет bbox для {col}"
    x, y, w, h = bbox
    print("dblclick генерирую", col, flush=True)
    # event_generate не принимает модификатор Double; Tk засчитывает двойной
    # клик по меткам времени — event generate с -time (32-битное, мс)
    now = int(tree.tk.call("clock", "milliseconds")) % (2 ** 31)
    for i in range(2):
        tree.tk.call("event", "generate", tree._w, "<Button-1>",
                     "-x", x + w // 2, "-y", y + h // 2,
                     "-time", now + i * 100)
    print("dblclick сгенерирован", flush=True)
    win.update()
    print("update после dblclick", flush=True)


def entry_open():
    win.update()

    def walk(w):
        for ch in w.winfo_children():
            if isinstance(ch, ttk.Entry):
                return ch
            r = walk(ch)
            if r is not None:
                return r

    return walk(win)


def type_in_entry(entry, text):
    print("ввод: вошёл в функцию", flush=True)
    exists = entry.tk.call("winfo", "exists", entry._w)
    print("ввод: exists =", exists, flush=True)
    entry.delete(0, "end")
    print("ввод: удалил", flush=True)
    entry.insert(0, text)
    print("ввод: вставил", flush=True)
    entry.event_generate("<Return>")
    print("ввод: Return сгенерирован", flush=True)
    win.update()
    print("ввод: update прошёл", flush=True)


def check_focus(entry, where):
    win.update()
    focused = win.tk.call("focus", "-displayof", win._w)
    ok = str(focused) == str(entry._w)
    print("фокус", where, ":", repr(str(focused)), "==", repr(str(entry._w)),
          "->", ok, "| autosave вызовов:", len(saved), flush=True)
    if not ok:
        raise SystemExit("ФОКУС НЕ В ПОЛЕ")
def row_values(iid="0"):
    return tree.item(iid, "values")


def find_button(text):
    def walk(w):
        for ch in w.winfo_children():
            if isinstance(ch, ttk.Button) and ch.cget("text") == text:
                return ch
            r = walk(ch)
            if r is not None:
                return r
    return walk(win)


# 1) исходное состояние
v = row_values()
print("исходная строка:", v)
assert v[1] == "Event" and v[2] == "complete the location details"

# 2) щелчок по «Уведомл.» выключает notify
click("notify")
v = row_values()
print("после клика по notify:", v)
assert v[4] == "—", "notify не переключился"
assert saved and saved[-1][0]["notify"] is False, "нет автосохранения"

# 3) щелчок по каналу переключает его
click("channel")
v = row_values()
print("после клика по каналу:", v)
assert v[1] != "Event", "канал не переключился"

# 4) двойной щелчок по подстроке → редактирование в ячейке
e = None
for attempt in range(5):
    dblclick("pattern")
    e = entry_open()
    if e is not None:
        break
assert e is not None, "поле ввода не открылось"
check_focus(e, "после двойного клика")
print("перед вводом: autosave вызовов:", len(saved), flush=True)
type_in_entry(e, "wtb rare")
v = row_values()
print("после правки подстроки:", v, "| saved:", saved[-1], flush=True)
assert v[2] == "wtb rare", "подстрока не изменилась"
assert saved[-1][0]["pattern"] == "wtb rare"

# 5) «Новый» → сразу открывается ввод подстроки
print("ищу кнопку", repr(m.tr("alerts_add")), flush=True)
btn_add = find_button(m.tr("alerts_add"))
print("кнопка:", btn_add, flush=True)
btn_add.invoke()
e = None
for attempt in range(5):
    win.update()
    e = entry_open()
    if e is not None:
        break
assert e is not None, "после «Новый» ввод не открылся"
check_focus(e, "после «Новый»")
type_in_entry(e, "hello world")
ids = tree.get_children()
print("строк после добавления:", len(ids), row_values(str(len(ids) - 1)), flush=True)
assert len(ids) == 2, "правило не добавилось"
assert saved[-1][1]["pattern"] == "hello world"

# 6) «Удалить» удаляет выбранное
print("шаг 6", flush=True)
tree.selection_set("0")
btn_del = find_button(m.tr("alerts_remove"))
btn_del.invoke()
win.update()
ids = tree.get_children()
print("строк после удаления:", len(ids), row_values("0"), flush=True)
assert len(ids) == 1, "правило не удалилось"
assert saved[-1][0]["pattern"] == "hello world"

# 7) «Закрыть» сохраняет и уничтожает окно
print("шаг 7", flush=True)
btn_close = find_button(m.tr("btn_close"))
btn_close.invoke()
win.update()
alive = bool(int(win.tk.call("winfo", "exists", win._w)))
print("окно закрыто:", not alive, "| итог в cfg:", cfg["alerts"], flush=True)
assert not alive, "окно не закрылось"
assert cfg["alerts"][0]["pattern"] == "hello world"

print("OK")
root.destroy()
