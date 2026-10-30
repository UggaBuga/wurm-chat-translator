# -*- coding: utf-8 -*-
"""Пробник: строит настоящее окно приложения и печатает геометрию
панели вкладок (tabbar, строки, кнопки каналов)."""
import sys
import tkinter as tk

sys.path.insert(0, r"E:\KimiData\kimi\Workspaces\1с тест умений\wurm_chat_translator")
import wurm_chat_gui as w


def probe(root):
    def walk(widget, depth=0):
        try:
            text = widget.cget("text")
        except Exception:
            text = ""
        info = (f"{'  ' * depth}{widget.winfo_class()}"
                f" h={widget.winfo_height()} reqh={widget.winfo_reqheight()}"
                f" w={widget.winfo_width()} mapped={widget.winfo_ismapped()}")
        if text:
            info += f" text={text!r}"
        print(info, flush=True)
        for child in widget.winfo_children():
            walk(child, depth + 1)

    walk(root)
    root.destroy()


_orig_tk = tk.Tk


class ProbeTk(_orig_tk):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.after(3500, lambda: probe(self))


tk.Tk = ProbeTk
w.run_gui()
print("=== пробник завершён ===", flush=True)
