# -*- coding: utf-8 -*-
"""Тест переноса вкладок каналов на строки при нехватке ширины."""
import sys
import tkinter as tk

sys.path.insert(0, r"E:\KimiData\kimi\Workspaces\1с тест умений\wurm_chat_translator")
import wurm_chat_gui as w


def make_state(root, names):
    bar = tk.Frame(root)
    bar.pack(fill="x")
    state = {"bar": bar, "groups": [], "rows": [], "width": 0, "job": None}
    for name in names:
        b = tk.Label(bar, text=name, pady=3, font=("Segoe UI", 8))
        s = tk.Label(bar, text="\U0001F50A", font=("Segoe UI Emoji", 7))
        n = tk.Label(bar, text="\U0001F514", font=("Segoe UI Emoji", 7))
        state["groups"].append((b, s, n))
    return state


def mapped_rows(state):
    return [r for r in state["rows"] if r.winfo_ismapped()]


def relayout(root, state, geo):
    root.geometry(geo)
    root.update()   # применить геометрию до измерения
    state["width"] = 0
    w.layout_tab_rows(state)
    root.update()


def main():
    root = tk.Tk()
    root.title("tab wrap test")
    names = [n for n, _, _ in w.CHANNELS]
    state = make_state(root, names)
    total = 3 * len(names)

    relayout(root, state, "300x200")
    rows_narrow = len(mapped_rows(state))
    placed = sum(len(r.pack_slaves()) for r in state["rows"])

    relayout(root, state, "2000x200")
    rows_wide = len(mapped_rows(state))

    relayout(root, state, "400x200")
    rows_back = len(mapped_rows(state))

    # регрессия: строки не должны закрывать кнопки. Порядок наложения =
    # порядок в winfo_children (снизу вверх): каждая отображаемая строка
    # обязана быть НИЖЕ всех кнопок, иначе пустые строки закрашивают вкладки.
    order = state["bar"].winfo_children()
    labels = [w_ for grp in state["groups"] for w_ in grp]
    top_label = max(order.index(lb) for lb in labels)
    stacked = all(order.index(row) < top_label
                  for row in mapped_rows(state))
    root.destroy()

    ok = True
    if not (rows_narrow > 1 and placed == total):
        print(f"FAIL: узкое окно — строк {rows_narrow} (нужно >1), "
              f"виджетов {placed}/{total}")
        ok = False
    if rows_wide != 1:
        print(f"FAIL: широкое окно — строк {rows_wide}, нужна 1")
        ok = False
    if rows_back < 2:
        print(f"FAIL: после сужания — строк {rows_back}, нужно >=2")
        ok = False
    if not stacked:
        print("FAIL: строки лежат ВЫШЕ кнопок во стеке — закрашивают их")
        ok = False
    print("OK" if ok else "FAIL")
    print(f"  узко (300px): {rows_narrow} строк; широко (2000px): "
          f"{rows_wide} строк; снова узко (400px): {rows_back} строк; "
          f"порядок стека: {'верно' if stacked else 'НЕВЕРНО'}")


if __name__ == "__main__":
    main()
