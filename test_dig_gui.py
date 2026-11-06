# -*- coding: utf-8 -*-
"""Дымовая проверка кнопки «Порыться в прошлом»: диалог, выгрузка заданного
числа строк до запуска в начало чата, раскраска «прошлого», граница запуска
(живые строки в блок не попадают). Гоняет реальный GUI через поддельный
mainloop; окно мелькает на экране."""
import os
import sys
import time
import datetime
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wurm_chat_gui as m
import tkinter as tk
from tkinter import ttk

tmp = tempfile.mkdtemp(prefix="wurm_dig_")
logs = os.path.join(tmp, "players", "TestChar", "logs")
os.makedirs(logs)
m.CONFIG_FILE = os.path.join(tmp, "cfg.json")
m.DEBUG_FILE = os.path.join(tmp, "dbg.log")
# перевод выключен: без сети, «прошлые» строки придут с translated=None
m.DEFAULTS = dict(m.DEFAULTS, clients=[{"name": "TestChar", "path": logs}],
                  translate=False)

month = f"{datetime.datetime.now().year}-{datetime.datetime.now().month:02d}"
log_path = os.path.join(logs, f"_Local.{month}.txt")
# «прошлое»: 5 строк, записаны ДО запуска приложения
with open(log_path, "w", encoding="utf-8") as f:
    for i in range(1, 6):
        f.write(f"[21:0{i}:00] <Alice> old msg {i}\n")

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
    while script and self.winfo_exists():
        try:
            self.update()
        except tk.TclError:
            break
        time.sleep(0.02)


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
        if isinstance(w, tk.Toplevel) and w.title().startswith("Порыться"):
            return w
    return None


def local_text(root):
    """Text-виджет вкладки Local (тот, куда падает живое сообщение)."""
    for w in walk(root):
        if isinstance(w, tk.Text) and "live msg here" in w.get("1.0", "end"):
            return w
    return None


# --- шаги сценария ---
def step_live_line(root):
    """Живая строка ПОСЛЕ запуска — в «прошлое» попасть не должна."""
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("[21:50:00] <Bob> live msg here\n")
    check("живая строка записана", True)


def step_wait_watcher(_root):
    check("пауза — наблюдатель подхватил живую строку", True)


def step_open_dig(root):
    b = find_button(root, "🕑 Прошлое")
    check("кнопка «🕑 Прошлое» найдена", b is not None)
    if b is None:
        return

    def interact():
        win = find_dialog(root)
        check("диалог открылся", win is not None)
        if win is None:
            print("DBG toplevels:", [w.title() for w in walk(root)
                                     if isinstance(w, tk.Toplevel)])
            return
        spin = next((w for w in walk(win)
                     if isinstance(w, ttk.Spinbox)), None)
        check("поле числа найдено", spin is not None)
        if spin is None:
            print("DBG children:", [type(w).__name__ for w in walk(win)])
        if spin is not None:
            spin.delete(0, "end")
            spin.insert(0, "3")
        ok = next((w for w in walk(win)
                   if isinstance(w, ttk.Button)
                   and w.cget("text") == "Показать"), None)
        check("кнопка «Показать» найдена", ok is not None)
        if ok is not None:
            ok.invoke()

    root.after(80, interact)
    b.invoke()          # блокируется в wait_window диалога — interact сработает
    root.update()
    check("диалог закрылся", find_dialog(root) is None)


def step_noop(_root):
    pass


def step_check_start(root):
    txt = local_text(root)
    check("виджет Local с живой строкой найден", txt is not None)
    if txt is None:
        return
    content = txt.get("1.0", "end")
    check("заголовок «прошлого» выведен", "⏳ Прошлое" in content)
    check("взяты ровно 3 последних: old msg 3 есть", "old msg 3" in content)
    check("old msg 4 есть", "old msg 4" in content)
    check("old msg 5 есть", "old msg 5" in content)
    check("old msg 1 не попал", "old msg 1" not in content)
    check("живая строка на месте", "live msg here" in content)
    root._dig_txt = txt


def step_check_past_style(root):
    txt = getattr(root, "_dig_txt", None)
    if txt is None:
        check("стиль: виджет недоступен", False)
        return
    ranges = txt.tag_ranges("past")
    check("тег past проставлен", bool(ranges))
    check("тег past_head проставлен", bool(txt.tag_ranges("past_head")))
    # живая строка НЕ должна оказаться внутри блока «прошлого»
    pos = txt.search("live msg here", "1.0")
    inside = False
    if pos:
        live_idx = txt.index(f"{pos} linestart")
        r = list(ranges)
        for i in range(0, len(r) - 1, 2):
            if txt.compare(r[i], "<=", live_idx) and \
                    txt.compare(live_idx, "<", r[i + 1]):
                inside = True
                break
    check("живая строка вне блока «прошлого»", bool(pos) and not inside)
    # заголовок идёт раньше первой «прошлой» строки (вставка в начало)
    head = txt.search("⏳ Прошлое", "1.0")
    first_old = txt.search("old msg 3", "1.0")
    check("блок «прошлого» в начале чата",
          bool(head) and bool(first_old)
          and txt.compare(head, "<", first_old))


script = [step_noop, step_noop,            # ждём автостарт слежения (+300 мс)
          step_live_line, step_wait_watcher, step_open_dig,
          step_noop, step_noop, step_noop, step_noop,
          step_check_start, step_check_past_style]
m.run_gui()

failed = [n for n, ok in results if not ok]
if not results:
    print("НИ ОДНОЙ проверки не выполнено — сценарий не отработал")
    sys.exit(1)
print(f"Выполнено проверок: {len(results)}")
print("Итог:", "все проверки пройдены" if not failed else f"ошибки: {failed}")
sys.exit(1 if failed else 0)
