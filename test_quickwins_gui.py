# -*- coding: utf-8 -*-
"""Дымовая проверка UX-быстрых побед 1.7.7.10:
- пустое состояние канала: подсказка вместо белого листа, снимается
  при первом сообщении;
- плейсхолдеры полей панели ответа (ввод — свой текст, вывод — подсказка
  о переводе), плейсхолдер ввода снимается фокусом и не уходит в перевод;
- переименования кнопок (Пауза / Срабатывания);
- номер версии только в заголовке, не в статусной строке.
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

tmp = tempfile.mkdtemp(prefix="wurm_smoke_qw_")
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


def find_button(win, text):
    for w in walk(win):
        if isinstance(w, ttk.Button) and w.cget("text") == text:
            return w
    return None


def find_scrolledtexts(win):
    import tkinter.scrolledtext as st
    return [w for w in walk(win) if isinstance(w, st.ScrolledText)]


def step_initial(root):
    # 1. подсказка пустого состояния есть в каждом канале
    texts = find_scrolledtexts(root)
    check("текстовых областей каналов найдено", len(texts) > 0)
    hinted = [t for t in texts if t.tag_ranges("empty_hint")]
    check("подсказка пустого состояния стоит во всех каналах",
          len(hinted) == len(texts) and len(texts) > 0)
    if hinted:
        first = hinted[0]
        check("текст подсказки — из i18n",
              m.tr("chat_empty_hint", ch="Local").split("…")[0]
              in first.get("1.0", "end"))

    # 2. плейсхолдеры панели ответа. Поле ответа отличаем от строки поиска
    # шириной (32), поле вывода — состоянием readonly.
    entries = [w for w in walk(root)
               if isinstance(w, ttk.Entry)
               and str(w.cget("state")) == "normal"
               and str(w.cget("width")) == "32"]
    outs = [w for w in walk(root)
            if isinstance(w, ttk.Entry) and str(w.cget("state")) == "readonly"
            and not isinstance(w, ttk.Combobox)]
    check("поле ввода ответа одно", len(entries) == 1)
    check("поле вывода перевода одно", len(outs) == 1)
    if entries and outs:
        check("ввод: плейсхолдер «Ваш текст…»",
              entries[0].get() == m.tr("reply_placeholder"))
        check("вывод: плейсхолдер «перевод появится…»",
              outs[0].get() == m.tr("reply_result_placeholder"))
        # фокус снимает плейсхолдер и НЕ запускает перевод подсказки
        entries[0].focus_set()
        root.update()
        check("фокус снял плейсхолдер (поле пустое)",
              entries[0].get() == "")
        check("плейсхолдер не ушёл в перевод (вывод — по-прежнему подсказка)",
              outs[0].get() == m.tr("reply_result_placeholder"))

    # 3. переименования кнопок: «Стоп» → «Пауза» (текст меняется на ходу:
    # до старта слежения кнопка называется «Старт»)
    check("кнопка пуск/пауза на месте",
          find_button(root, m.tr("btn_stop")) is not None
          or find_button(root, m.tr("btn_start")) is not None)
    check("кнопка журнала сигналов переименована",
          find_button(root, m.tr("alerts_log_btn")) is not None
          and find_button(root, "Log") is None)

    # 4. версия не дублируется в статусной строке
    labels = [w for w in walk(root) if isinstance(w, ttk.Label)]
    ver_labels = [l for l in labels
                  if str(l.cget("text")).startswith("v" + m.APP_VERSION)]
    check("номер версии только в заголовке окна",
          len(ver_labels) == 0)


script = [step_initial]
m.run_gui()

bad = [n for n, ok in results if not ok]
print(f"\nВыполнено проверок: {len(results)}")
if bad:
    print("ПРОВАЛЕНО:")
    for n in bad:
        print("  -", n)
    sys.exit(1)
print("Итог: все проверки пройдены")
