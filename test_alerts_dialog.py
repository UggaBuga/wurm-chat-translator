"""Standalone-проверка логики диалога «Сигналы»: симулируем клики."""
import tkinter as tk
from tkinter import ttk
import importlib.util

spec = importlib.util.spec_from_file_location("w", "wurm_chat_gui.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

tr = lambda k: k
cfg = {"alerts": [{"channel": "Event", "pattern": "complete the location details",
                   "enabled": True, "sound": True, "notify": True}]}
saved = []

def save_config(c):
    saved.append(dict(c))

CHANNELS = w.CHANNELS
root = tk.Tk()
root.withdraw()

# --- копия тела open_alerts_dialog (та же логика, что в приложении) ---
win = tk.Toplevel(root)
rules = [dict(r) for r in (cfg.get("alerts") or []) if isinstance(r, dict)]

tree = ttk.Treeview(win, columns=("on", "channel", "pattern", "sound", "notify"),
                    show="headings", height=8, selectmode="browse")
tree.pack(fill="both", expand=True)

mark = lambda b: "+" if b else "-"

def refill():
    for iid in tree.get_children():
        tree.delete(iid)
    for i, r in enumerate(rules):
        tree.insert("", "end", iid=str(i), values=(
            mark(bool(r.get("enabled", True))),
            r.get("channel") or "any",
            r.get("pattern", ""),
            mark(bool(r.get("sound"))),
            mark(bool(r.get("notify")))))

form = ttk.Frame(win); form.pack(fill="x")
ch_names = [name for name, _, _ in CHANNELS]
ch_var = tk.StringVar(value=ch_names[0])
ttk.Combobox(form, textvariable=ch_var, state="readonly",
             values=["any"] + ch_names, width=11).pack(side="left")
pat_var = tk.StringVar()
pat_entry = ttk.Entry(form, textvariable=pat_var, width=28)
pat_entry.pack(side="left")
on_var = tk.BooleanVar(value=True); a_snd_var = tk.BooleanVar(value=True)
a_ntf_var = tk.BooleanVar(value=False)

def load_fields(_e=None):
    sel = tree.selection()
    if not sel:
        return
    r = rules[int(sel[0])]
    ch_var.set(r.get("channel") or "any")
    pat_var.set(r.get("pattern", ""))
    on_var.set(bool(r.get("enabled", True)))
    a_snd_var.set(bool(r.get("sound")))
    a_ntf_var.set(bool(r.get("notify")))

tree.bind("<Double-Button-1>", load_fields)
tree.bind("<<TreeviewSelect>>", load_fields)

def save_rule():
    pattern = pat_var.get().strip()
    if not pattern:
        print("save_rule: ПУСТАЯ подстрока — молча вышел")
        return
    ch = ch_var.get()
    rule = {"channel": "" if ch == "any" else ch, "pattern": pattern,
            "enabled": bool(on_var.get()), "sound": bool(a_snd_var.get()),
            "notify": bool(a_ntf_var.get())}
    sel = tree.selection()
    if sel and int(sel[0]) < len(rules):
        rules[int(sel[0])] = rule
    else:
        rules.append(rule)
    refill()
    tree.selection_set(str(rules.index(rule)))

def new_rule():
    pat_var.set("")
    on_var.set(True); a_snd_var.set(True); a_ntf_var.set(False)
    tree.selection_remove(tree.selection())
    pat_entry.focus_set()

def remove_rule():
    for sel in sorted((int(s) for s in tree.selection()), reverse=True):
        if 0 <= sel < len(rules):
            del rules[sel]
    refill()

def on_close():
    cfg["alerts"] = rules
    save_config(cfg)
    win.destroy()

refill()
# --- сценарий 1: редактирование существующего ---
tree.selection_set("0")
win.update()
print("поля после выбора:", repr(pat_var.get()), ch_var.get())
pat_var.set("complete the LOCATION")
save_rule()
win.update()
print("после save_rule:", rules[0]["pattern"], "| rows:", tree.get_children())

# --- сценарий 2: создание нового ---
new_rule()
pat_var.set("wtb rare")
save_rule()
win.update()
print("после добавления:", len(rules), "правил, последнее:", rules[-1])

# --- сценарий 3: удаление ---
tree.selection_set("1")
remove_rule()
print("после удаления:", len(rules))
on_close()
print("сохранено в конфиг:", len(saved) == 1 and len(saved[0]["alerts"]))
root.destroy()
print("OK")
