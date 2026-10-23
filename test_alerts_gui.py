# -*- coding: utf-8 -*-
"""GUI-тест диалога «Сигналы на сообщения»: исполняет РЕАЛЬНЫЙ код функции
open_alerts_dialog, извлечённый из wurm_chat_gui.py через ast, и симулирует
мышь/клавиатуру. Проверяет: переключение флажка, выпадающий чек-лист каналов
(применяется сразу, не закрывается при выборе, закрывается кликом мимо и по
Escape), правку подстроки двойным щелчком, добавление и удаление правила,
автосохранение."""
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
      "save_config": save_config, "CHANNELS": m.CHANNELS, "root": root,
      "rule_channels": m.rule_channels}
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
    assert bbox, f"нет bbox для {col}"
    x, y, w, h = bbox
    tree.event_generate("<Button-1>", x=x + w // 2, y=y + h // 2)
    root.update()
    win.update()


def dblclick(col):
    bbox = tree.bbox("0", col)
    assert bbox, f"нет bbox для {col}"
    x, y, w, h = bbox
    # event_generate не принимает модификатор Double; Tk засчитывает двойной
    # клик по меткам времени — event generate с -time (32-битное, мс)
    now = int(tree.tk.call("clock", "milliseconds")) % (2 ** 31)
    for i in range(2):
        tree.tk.call("event", "generate", tree._w, "<Button-1>",
                     "-x", x + w // 2, "-y", y + h // 2,
                     "-time", now + i * 100)
    win.update()


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
    entry.delete(0, "end")
    entry.insert(0, text)
    entry.event_generate("<Return>")
    win.update()


def check_focus(entry, where):
    win.update()
    focused = win.tk.call("focus", "-displayof", win._w)
    assert str(focused) == str(entry._w), f"ФОКУС НЕ В ПОЛЕ ({where})"


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


def find_dropdown():
    """Выпадающий чек-лист — tk.Listbox, размещённый через place."""
    found = []

    def walk(w):
        for ch in w.winfo_children():
            if isinstance(ch, tk.Listbox):
                found.append(ch)
            walk(ch)

    walk(win)
    return found


# 1) исходное состояние
v = row_values()
assert v[1] == "Event" and v[2] == "complete the location details"

# 2) щелчок по «Уведомл.» выключает notify
click("notify")
v = row_values()
assert v[4] == "—", "notify не переключился"
assert saved and saved[-1][0]["notify"] is False, "нет автосохранения"

# 3) щелчок по каналу открывает выпадающий чек-лист внутри окна
click("channel")
win.update()
drops = find_dropdown()
assert len(drops) == 1, f"чек-лист не открылся: {len(drops)}"
dd = drops[0]

names = [m.tr("alert_any")] + [n for n, _, _ in m.CHANNELS]


def dd_items():
    return [dd.get(i) for i in range(dd.size())]


items = dd_items()
assert any(m.tr("alert_any") in it for it in items), "нет пункта «Любой»"
assert any("Local" in it for it in items), "нет пункта «Local»"
assert any("Event" in it and "☑" in it for it in items), \
    "Event не отмечен галочкой"


def toggle(name):
    """Клик по пункту списка — синтетическим событием по bbox."""
    idx = names.index(name)
    bbox = dd.bbox(idx)
    assert bbox, f"нет bbox для пункта {name}"
    x, y, w, h = bbox
    dd.event_generate("<Button-1>", x=x + w // 2, y=y + h // 2)
    root.update()
    win.update()


toggle("Village")
assert find_dropdown(), "список закрылся после выбора канала!"
v = row_values()
assert v[1] == "Village, Event", f"ожидалось 'Village, Event', получено {v[1]!r}"
assert saved[-1][0]["channels"] == ["Village", "Event"], "channels не сохранились"
assert "channel" not in saved[-1][0], "старый ключ channel не удалён"
toggle("Local")
assert find_dropdown(), "список закрылся после выбора канала!"
v = row_values()
assert v[1] == "Local, Village, Event", \
    f"ожидалось 'Local, Village, Event', получено {v[1]!r}"
assert saved[-1][0]["channels"] == ["Local", "Village", "Event"]
toggle("Village")                # снять галочку — Village уходит
v = row_values()
assert v[1] == "Local, Event", f"ожидалось 'Local, Event', получено {v[1]!r}"
assert saved[-1][0]["channels"] == ["Local", "Event"]
assert find_dropdown(), "список закрылся до закрытия"

# «Любой» сбрасывает выбранные каналы
toggle(m.tr("alert_any"))
v = row_values()
assert v[1] == m.tr("alert_any"), f"ожидался «Любой», получено {v[1]!r}"
assert saved[-1][0]["channels"] == [], "channels не сброшены"
toggle("Event")                  # вернуть Event как было
v = row_values()
assert v[1] == "Event"
assert find_dropdown(), "список закрылся"

# 3б) Escape закрывает список; повторный щелчок по каналу открывает снова
win.event_generate("<Escape>")
win.update()
assert not find_dropdown(), "список не закрылся по Escape"
click("channel")
win.update()
assert find_dropdown(), "список не переоткрылся по щелчку"
# клик мимо закрывает список, но сам щелчок отрабатывает (флажок переключится)
click("notify")
win.update()
assert not find_dropdown(), "список не закрылся по клику мимо"
v = row_values()
assert v[4] == "✓", "клик мимо не должен съедать щелчок по флажку"
click("notify")                   # вернуть как было
win.update()

# 4) двойной щелчок по подстроке → редактирование в ячейке
e = None
for attempt in range(5):
    dblclick("pattern")
    e = entry_open()
    if e is not None:
        break
assert e is not None, "поле ввода не открылось"
check_focus(e, "после двойного клика")
type_in_entry(e, "wtb rare")
v = row_values()
assert v[2] == "wtb rare", "подстрока не изменилась"
assert saved[-1][0]["pattern"] == "wtb rare"

# 5) «Новый» → сразу открывается ввод подстроки
btn_add = find_button(m.tr("alerts_add"))
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
assert len(ids) == 2, "правило не добавилось"
assert saved[-1][1]["pattern"] == "hello world"

# 6) «Удалить» удаляет выбранное
tree.selection_set("0")
btn_del = find_button(m.tr("alerts_remove"))
btn_del.invoke()
win.update()
ids = tree.get_children()
assert len(ids) == 1, "правило не удалилось"
assert saved[-1][0]["pattern"] == "hello world"

# 7) «Закрыть» сохраняет и уничтожает окно
btn_close = find_button(m.tr("btn_close"))
btn_close.invoke()
win.update()
alive = bool(int(win.tk.call("winfo", "exists", win._w)))
assert not alive, "окно не закрылось"
assert cfg["alerts"][0]["pattern"] == "hello world"

print("OK")
root.destroy()
