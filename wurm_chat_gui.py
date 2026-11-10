#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Wurm Chat GUI — оконный переводчик чатов Wurm Online.

На основе wurm_log_watcher_ru.py. Следит за логами чата (файлы текущего месяца):

    _Local.2026-10.txt      — локальный чат
    Village.2026-10.txt     — деревня          Alliance.2026-10.txt — альянс
    Trade.2026-10.txt       — торговля         Freedom.2026-10.txt  — Freedom
    GL-Freedom.2026-10.txt  — глобал Freedom   Russian_GL.2026-10.txt — русский глобал
    CA_HELP.2026-10.txt     — помощь новичкам
    PM__<Игрок>.2026-10.txt — личные сообщения

Каждому чату отведена своя вкладка-окно с лентой сообщений.
Новые сообщения переводятся на русский через Google gtx (sl=auto),
имена персонажей <в угловых скобках> не переводятся, в конце перевода
добавляется код исходного языка, напр. " (en)".

Запуск:
    python wurm_chat_gui.py

Настройки (папка логов, флаги) сохраняются в wurm_chat_gui_config.json
рядом со скриптом.
"""

from __future__ import annotations

import datetime
import json
import os
import platform
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.sax.saxutils

try:
    import pystray
    from PIL import Image, ImageDraw
    HAVE_TRAY = True
except Exception:                     # pystray не обязателен — без него просто выход
    HAVE_TRAY = False

APP_TITLE = "Wurm Chat — переводчик"
APP_VERSION = "1.7.7.7"
# В exe (PyInstaller) __file__ живёт во временной папке распаковки — пути к
# конфигу и debug-логу берём рядом с исполняемым файлом.
APP_DIR = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
           else os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(APP_DIR, "wurm_chat_gui_config.json")
DEBUG_FILE = os.path.join(APP_DIR, "wurm_chat_gui_debug.log")

# Один экземпляр приложения: именованный мьютекс + триггер-файл, по которому
# уже запущенный экземпляр показывает своё окно (в т.ч. из трея).
MUTEX_NAME = "Global\\WurmChatTranslator_SingleInstance_v1"
ACTIVATE_TRIGGER = os.path.join(APP_DIR, ".wurm_chat_activate")
# Смена языка интерфейса пересоздаёт окно: run_gui поднимает флаг, main
# запускает его заново (цикл вместо рекурсии — стек не растёт).
_RESTART_REQUESTED = {"v": False}


def _single_instance_mutex():
    """Создать мьютекс. Вернуть (handle, already_running)."""
    import ctypes
    # use_last_error=True — иначе GetLastError не срабатывает и повторный
    # запуск не определяется.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    already = (ctypes.get_last_error() == 183)  # ERROR_ALREADY_EXISTS
    return handle, already


def _find_existing_hwnd():
    """HWND видимого (или свёрнутого в трей) окна другого экземпляра."""
    import ctypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, l):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(h, buf, 256)
        if buf.value.startswith(APP_TITLE):
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            if pid.value and pid.value != os.getpid():
                found.append(h)
        return True

    user32.EnumWindows(cb, None)
    return found[0] if found else None


def _activate_existing():
    """Попросить уже запущенный экземпляр показать окно: создать триггер-файл,
    работающий цикл poll() его заметит и развернёт окно (в т.ч. из трея)."""
    try:
        with open(ACTIVATE_TRIGGER, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _terminate_existing():
    """Завершить другие экземпляры приложения (для «заменить»)."""
    import ctypes
    kernel32 = ctypes.windll.kernel32
    hwnd = _find_existing_hwnd()
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    SYNCHRONIZE = 0x00100000
    PROCESS_TERMINATE = 0x0001
    h = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_TERMINATE, False, pid.value)
    if not h:
        return False
    kernel32.TerminateProcess(h, 0)
    kernel32.WaitForSingleObject(h, 5000)
    kernel32.CloseHandle(h)
    return True


def log_debug(msg):
    """Дописать строку в файл отладки — чтобы падения не исчезали бесследно."""
    try:
        with open(DEBUG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass


def bind_copy(widget):
    """Ctrl-C / Ctrl-Insert в read-only Text: Tk на Windows не копирует сам."""
    import tkinter as tk   # лениво: на глобальном уровне модуля tkinter нет
    def _copy(_event=None):
        try:
            sel = widget.get("sel.first", "sel.last")
        except tk.TclError:
            return None    # выделения нет — не мешаем дальнейшей обработке
        widget.clipboard_clear()
        widget.clipboard_append(sel)
        return "break"
    for seq in ("<Control-c>", "<Control-C>", "<Control-Insert>"):
        widget.bind(seq, _copy)


def _excepthook(exc_type, exc, tb):
    log_debug(f"НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ: {exc_type.__name__}: {exc}\n"
              + "".join(traceback.format_tb(tb)))


sys.excepthook = _excepthook

# Репозиторий проекта — для проверки обновлений
GITHUB_REPO = "UggaBuga/wurm-chat-translator"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases"
LATEST_EXE_URL = (f"https://github.com/{GITHUB_REPO}/releases/latest/"
                  f"download/WurmChatTranslator.exe")


def parse_version(text):
    """'v1.1.1' / '1.1.1' -> (1, 1, 1). Нечисловое -> None."""
    m = re.search(r"(\d+(?:\.\d+)+)", text or "")
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def check_latest_release(timeout=8):
    """Версия последнего релиза на GitHub или None (сеть/ошибка)."""
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        data = json.loads(_http_get(url, timeout=timeout))
        return parse_version(data.get("tag_name") or data.get("name"))
    except Exception as exc:
        log_debug(f"проверка обновлений: не удалось: {exc!r}")
        return None


def download_update(dest, timeout=240):
    """Скачать exe последнего релиза в dest. True — файл на месте и не пуст."""
    try:
        curl = _find_curl()
        if curl:
            cf = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            out = subprocess.run(
                [curl, "-sS", "-L", "--max-time", str(timeout), "-A",
                 "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                 "-o", dest, LATEST_EXE_URL],
                capture_output=True, creationflags=cf, timeout=timeout + 15)
            if out.returncode != 0:
                log_debug(f"автообновление: curl вернул {out.returncode}: "
                          f"{out.stderr.decode('utf-8', 'replace')[:200]}")
                return False
        else:                       # крайний случай — без curl, через urllib
            import urllib.request
            req = urllib.request.Request(
                LATEST_EXE_URL,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=timeout) as r, \
                    open(dest, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
        ok = os.path.isfile(dest) and os.path.getsize(dest) > 3_000_000
        if not ok and os.path.isfile(dest):
            os.remove(dest)
        return ok
    except Exception as exc:
        log_debug(f"автообновление: скачивание не удалось: {exc!r}")
        try:
            if os.path.isfile(dest):
                os.remove(dest)
        except OSError:
            pass
        return False


def _ps_quote(path):
    """Путь для вставки в одинарные кавычки PowerShell."""
    return path.replace("'", "''")


def build_update_script(exe, tmp, pid):
    """Скрипт помощника: дождаться выхода старой копии, подменить exe,
    запустить новую. При неудаче — вернуть старую обратно. Процесс к этому
    моменту может быть уже завершён — Get-Process/Wait-Process терпят это."""
    bak = exe + ".bak"
    return f"""$ErrorActionPreference = 'Stop'
$old = Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue
if ($old) {{ try {{ Wait-Process -Id {int(pid)} -Timeout 90 }} catch {{}} }}
Start-Sleep -Milliseconds 800
try {{
  $moved = $false
  for ($i = 0; $i -lt 20; $i++) {{
    try {{
      Move-Item -LiteralPath '{_ps_quote(exe)}' -Destination '{_ps_quote(bak)}' -Force
      $moved = $true
      break
    }} catch {{
      Start-Sleep -Milliseconds 500
    }}
  }}
  if (-not $moved) {{ throw 'cannot replace exe' }}
  Move-Item -LiteralPath '{_ps_quote(tmp)}' -Destination '{_ps_quote(exe)}' -Force
  Start-Process '{_ps_quote(exe)}'
  Remove-Item -LiteralPath '{_ps_quote(bak)}' -Force -ErrorAction SilentlyContinue
}} catch {{
  if (Test-Path -LiteralPath '{_ps_quote(bak)}') {{
    Move-Item -LiteralPath '{_ps_quote(bak)}' -Destination '{_ps_quote(exe)}' -Force -ErrorAction SilentlyContinue
  }}
}}
Remove-Item $PSCommandPath -Force -ErrorAction SilentlyContinue
"""


def spawn_update_helper(exe, tmp):
    """Запустить отсоединённого помощника, который дождётся нашего выхода,
    заменит exe свежескачанным и стартанёт его. True — помощник запущен."""
    try:
        script = build_update_script(exe, tmp, os.getpid())
        fd, ps1 = tempfile.mkstemp(prefix="wct_update_", suffix=".ps1")
        # BOM обязателен: Windows PowerShell 5.1 читает ps1 без BOM как ANSI
        # и портит кириллицу в путях (скрипт падает, замена не происходит)
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(script)
        # DETACHED_PROCESS нельзя: PowerShell без консоли молча не стартует.
        # NO_WINDOW + NEW_PROCESS_GROUP — помощник живёт сам по себе.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) \
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden",
             "-ExecutionPolicy", "Bypass", "-File", ps1],
            creationflags=flags, close_fds=True)
        return True
    except Exception as exc:
        log_debug(f"автообновление: помощник не запущен: {exc!r}")
        return False

# Корни установки Wurm Online: Steam-версия и автономный клиент с сайта
# (у него данные лежат в домашней папке пользователя). Подпапка игрока
# подставляется автоматически. Используется только по явному запросу
# (кнопка «Найти») — сохранённый пользователем путь никогда не подменяется.
WURM_PLAYERS_ROOTS = [
    r"e:\SteamLibrary\steamapps\common\Wurm Online\gamedata\players",
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 r"Steam\steamapps\common\Wurm Online\gamedata\players"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                 r"Steam\steamapps\common\Wurm Online\gamedata\players"),
    r"D:\SteamLibrary\steamapps\common\Wurm Online\gamedata\players",
    r"C:\SteamLibrary\steamapps\common\Wurm Online\gamedata\players",
    # Автономный клиент (установщик с wurmonline.com): данные в профиле
    os.path.join(os.path.expanduser("~"), "wurm", "players"),
    os.path.join(os.path.expanduser("~"), "Wurm", "players"),
    os.path.join(os.path.expanduser("~"), "Wurm Online", "players"),
    r"C:\Program Files (x86)\Wurm Online\wurm\players",
    r"C:\Program Files\Wurm Online\wurm\players",
]


def steam_persona_name():
    """Имя (PersonaName) текущего пользователя Steam из config\\loginusers.vdf.

    Приоритет — аккаунт с MostRecent=1; иначе последний вписанный в файл.
    """
    candidates = [
        r"C:\Steam\config\loginusers.vdf",
        os.path.join(os.environ.get("ProgramFiles(x86)",
                                    r"C:\Program Files (x86)"),
                     r"Steam\config\loginusers.vdf"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     r"Steam\config\loginusers.vdf"),
        r"D:\Steam\config\loginusers.vdf",
        r"E:\Steam\config\loginusers.vdf",
    ]
    text = None
    for vdf_path in candidates:
        try:
            with open(vdf_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            break
        except OSError:
            continue
    if text is None:
        return None
    recent = last = None
    for block in re.findall(r"\{[^{}]*\}", text):
        pm = re.search(r'"PersonaName"\s+"([^"]+)"', block)
        if not pm or not pm.group(1).strip():
            continue
        last = pm.group(1).strip()
        if re.search(r'"MostRecent"\s+"1"', block):
            recent = last
    return recent or last


def auto_logs_dir():
    """Путь к логам: ...\\players\\<игрок>\\logs — Steam и автономный клиент.

    1) подпапка, совпадающая с PersonaName Steam (без учёта регистра);
    2) иначе персонаж с самой свежей активностью в логах (кто реально играл
       последним — по дате последнего записанного файла в его logs);
    3) иначе единственная подпапка.
    Ничего не нашлось — пустая строка (пользователь укажет путь вручную).
    Вызывается только по явному запросу (кнопка «Найти»), никогда сама.
    """
    for players_root in WURM_PLAYERS_ROOTS:   # все корни, не только первый —
        if not os.path.isdir(players_root):   # Steam может отсутствовать,
            continue                            # а автономный клиент — присутствовать
        subs = [os.path.join(players_root, d)
                for d in os.listdir(players_root)
                if os.path.isdir(os.path.join(players_root, d))]
        if not subs:
            continue

        def logs_of(d):
            logs = os.path.join(d, "logs")
            return logs if os.path.isdir(logs) else None

        persona = steam_persona_name()
        if persona:
            persona_lower = persona.lower()
            for d in subs:
                if os.path.basename(d).lower() == persona_lower:
                    logs = logs_of(d)
                    if logs:
                        log_debug(f"auto_logs_dir: игрок по PersonaName Steam '{persona}' ({players_root})")
                        return logs

        def last_activity(logs):
            try:
                return max(os.path.getmtime(os.path.join(logs, f))
                           for f in os.listdir(logs))
            except (ValueError, OSError):
                return -1

        best = max((logs_of(d) for d in subs),
                   key=lambda l: last_activity(l) if l else -1)
        if best:
            log_debug(f"auto_logs_dir: персонаж по активности логов: "
                      f"'{os.path.basename(os.path.dirname(best))}' ({players_root})")
            return best
    return ""


def find_all_logs_dirs():
    """Все найденные папки логов всех клиентов: [{'name', 'path'}].

    Подходит для мультиоконной игры: Steam + сайт-клиент + любые другие
    корни из WURM_PLAYERS_ROOTS. Имя — персонаж + тип клиента в скобках,
    при совпадении имён персонажей тип обязателен, иначе кратко.
    """
    candidates = []   # (персонаж, тип, путь)
    for root in WURM_PLAYERS_ROOTS:
        if not os.path.isdir(root):
            continue
        kind = tr("kind_steam") if "steamapps" in root.lower() else tr("kind_site")
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for d in names:
            logs = os.path.join(root, d, "logs")
            if os.path.isdir(logs):
                candidates.append((d, kind, logs))
    # имена персонажей, встречающиеся в нескольких клиентах, — помечаем типом
    from collections import Counter
    name_count = Counter(c[0].lower() for c in candidates)
    seen = set()
    result = []
    for char, kind, logs in candidates:
        key = logs.lower()
        if key in seen:
            continue
        seen.add(key)
        label = f"{char} ({kind})" if name_count[char.lower()] > 1 else char
        result.append({"name": label, "path": logs})
    return result

# Отслеживаемые каналы: (имя вкладки, префикс файла, это личка PM__<Имя>.<месяц>.txt)
CHANNELS = [
    ("Local", "_Local", False),
    ("Village", "Village", False),
    ("Alliance", "Alliance", False),
    ("Trade", "Trade", False),
    ("Freedom", "Freedom", False),
    ("GL-Freedom", "GL-Freedom", False),
    ("Russian GL", "Russian_GL", False),
    ("CA Help", "CA_HELP", False),
    ("Event", "_Event", False),
    ("PM", "PM__", True),
]

# Языковые каналы вида «Russian GL» — чаты на конкретном языке. По умолчанию
# не переводятся вообще (ни текст, ни валюта): опция translate_lang_channels.
RE_LANG_CHANNEL = re.compile(
    r"^(?:russian|english|german|french|spanish|portuguese|chinese|polish|"
    r"italian|dutch|turkish|japanese|korean|swedish|norwegian|danish|"
    r"finnish|czech|hungarian|romanian|ukrainian)\s+GL$", re.IGNORECASE)

GTX_URL = "https://translate.googleapis.com/translate_a/single"
GTX_TIMEOUT = 6
TRANSLATE_MAX_CHARS = 1000
TRANSLATE_CACHE_MAX = 5000
TRANSLATE_INTERVAL = 1.0  # пауза между запросами, чтобы Google не банил (429)

# Реальный формат логов Wurm Online:
#   [19:13:45] <Uggabuga> guards!              — обычные каналы и ЛС
#   [17:38:41] Uggabuga has reached 30 ... !   — системные строки без имени
# Тела ([\s\S], а не .) специально пропускают \n: многострочные сообщения
# игры лежат в логе несколькими физическими строками и собираются обратно
# reassemble_lines() до разбора.
RE_NAME = re.compile(r"^\[(?:\d{4}-\d{2}-\d{2} )?(\d{2}:\d{2}:\d{2})\]\s*<([^<>\r\n]+)>\s*([\s\S]*\S)\s*$")
RE_SYS = re.compile(r"^\[(?:\d{4}-\d{2}-\d{2} )?(\d{2}:\d{2}:\d{2})\]\s*([\s\S]+?\S)\s*$")
RE_COLON = re.compile(r"^\[(?:\d{4}-\d{2}-\d{2} )?(\d{2}:\d{2}:\d{2})\]\s*([^\s:\[\]]{2,30}):\s*([\s\S]*\S)\s*$")
# строка с меткой времени — начало нового сообщения (граница для сборки)
RE_TS_LINE = re.compile(r"^\[(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2}\]")

NAME_TOKEN_RE = re.compile(r"(<[^<>\r\n]+>)")


# --------------------------------------------------------------------------- #
# Перевод: основной бэкенд — Google gtx, запасной — MyMemory
# --------------------------------------------------------------------------- #

GTX_LAST_ERROR = None  # "429", если Google отказал по частоте запросов
# «Аварийный выключатель»: после серии 429 Google отключается на время,
# чтобы очередь не простаивала по 15+ секунд на каждом сообщении.
GTX_BANNED_UNTIL = 0.0       # monotonic, до которого gtx не дёргаем
GTX_FAILS = 0                # подряд идущих 429
GTX_BAN_THRESHOLD = 3        # столько 429 подряд → бан
GTX_BAN_SECONDS = 1800       # бан на 30 минут

# Состояние переводчика для индикатора в статусной строке:
# "gtx" | "mymemory" | "fallback" (gtx в бане, идёт через запасной) | None (нет данных)
TRANSLATOR_STATE = {"backend": None}


def translator_status_text():
    """Человекочитаемый статус переводчика для статусной строки."""
    st = TRANSLATOR_STATE.get("backend")
    if st == "gtx":
        return tr("backend_gtx")
    if st == "mymemory":
        return tr("backend_mymemory")
    if st == "fallback":
        return tr("backend_fallback")
    return tr("backend_unknown")


def _find_curl():
    """Путь к curl.exe: вложенный в exe (curlbin) → системный. Вложенный берём
    первым: системный curl Windows Google блокирует по TLS-отпечатку («Sorry…»),
    а вложенный (из комплекта) проходит."""
    base = getattr(sys, "_MEIPASS", None)   # папка распаковки onefile-exe
    candidates = []
    if base:
        candidates.append(os.path.join(base, "curlbin", "curl.exe"))
    candidates.append(os.path.join(APP_DIR, "curlbin", "curl.exe"))
    candidates.append(r"C:\Windows\System32\curl.exe")
    candidates.append("curl.exe")
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return None


def _http_get(url, timeout=GTX_TIMEOUT):
    """GET-запрос с текстовым ответом. Через curl (Google по TLS-отпечатку банит
    прямые HTTPS-запросы urllib — «Sorry…»), при отсутствии curl — urllib."""
    curl = _find_curl()
    if curl:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = dict(os.environ)
        # curl подбирает DLL рядом с собой — убираем путаницу из PATH не нужно,
        # Windows и так смотрит каталог exe в первую очередь.
        out = subprocess.run(
            [curl, "-sS", "--max-time", str(timeout), "-A",
             "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", url],
            capture_output=True, creationflags=creationflags,
            timeout=timeout + 5, env=env)
        if out.returncode == 0 and out.stdout:
            text = out.stdout.decode("utf-8", "replace")
            if not text.lstrip().startswith("<"):
                return text
            raise RuntimeError("curl: получена HTML-заглушка (блокировка)")
        raise RuntimeError(f"curl exit={out.returncode}: {out.stderr[:120]!r}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def google_gtx_translate_ex(text, dst="ru", src=None, timeout=GTX_TIMEOUT):
    """Перевод через Google gtx. Возвращает (перевод, исходный язык) или (None, None)."""
    global GTX_LAST_ERROR
    GTX_LAST_ERROR = None
    if not text:
        return text, "auto"
    if len(text) > TRANSLATE_MAX_CHARS:
        return None, None
    params = urllib.parse.urlencode({"client": "gtx", "sl": src or "auto",
                                     "tl": dst, "dt": "t", "q": text})
    try:
        data = json.loads(_http_get(f"{GTX_URL}?{params}", timeout=timeout))
        detected_src = "auto"
        if len(data) > 2 and isinstance(data[2], str) and data[2].strip():
            detected_src = data[2].strip()
        parts = [seg[0] for seg in data[0]
                 if isinstance(seg, list) and seg and isinstance(seg[0], str)]
        translated = "".join(parts).strip()
        return (translated or None), detected_src
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            GTX_LAST_ERROR = "429"   # Google ограничил частоту — надо подождать
        return None, None
    except Exception:
        return None, None


def mymemory_translate_ex(text, dst="ru", src=None, timeout=GTX_TIMEOUT):
    """Запасной перевод через MyMemory (без ключа; src=None — автоопределение)."""
    if not text or len(text) > 400:
        return None, None
    try:
        langpair = f"{src}|{dst}" if src else f"Autodetect|{dst}"
        url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(
            {"q": text, "langpair": langpair})
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        if str(data.get("responseStatus")) != "200":
            return None, None
        rd = data.get("responseData") or {}
        translated = (rd.get("translatedText") or "").strip()
        detected = (rd.get("detectedLanguage") or "").strip() or "auto"
        if "MYMEMORY WARNING" in translated:
            return None, None   # исчерпана дневная квота — это не перевод
        return (translated or None), detected
    except Exception:
        return None, None


def _mymemory_with_state(text, dst="ru", src=None, fallback=False):
    """MyMemory + обновление индикатора состояния переводчика."""
    translated, detected = mymemory_translate_ex(text, dst=dst, src=src)
    if translated is not None:
        TRANSLATOR_STATE["backend"] = "fallback" if fallback else "mymemory"
    return translated, detected


def translate_auto(text, dst="ru", src=None):
    """Цепочка бэкендов: Google gtx (с выключателем при 429) → MyMemory.
    src=None — исходный язык определяет сам переводчик."""
    global GTX_BANNED_UNTIL, GTX_FAILS
    if time.monotonic() < GTX_BANNED_UNTIL:
        return _mymemory_with_state(text, dst=dst, src=src, fallback=True)
    translated, detected = google_gtx_translate_ex(text, dst=dst, src=src)
    if translated is not None:
        GTX_FAILS = 0
        TRANSLATOR_STATE["backend"] = "gtx"
        return translated, detected
    if GTX_LAST_ERROR == "429":
        GTX_FAILS += 1
        if GTX_FAILS >= GTX_BAN_THRESHOLD:
            GTX_BANNED_UNTIL = time.monotonic() + GTX_BAN_SECONDS
            GTX_FAILS = 0
            log_debug(f"translate: gtx банит (429), перерыв {GTX_BAN_SECONDS // 60} мин, "
                      f"перевод через MyMemory")
        return _mymemory_with_state(text, dst=dst, src=src, fallback=True)
    return _mymemory_with_state(text, dst=dst, src=src)


def translate_preserving_names(text, dst="ru"):
    """Переводит текст, не трогая <ИмяПерсонажа>, и добавляет код языка в конце.

    Возвращает (текст, исходный язык); язык — реальный код либо None."""
    def norm(detected):
        # автоопределение на коротких/формульных фразах («QL:99 DMG:0.0»)
        # шалит и выдаёт «no», «ca» и т.п. — такой язык не поддерживаем,
        # принимаем за английский (основной язык игрового чата)
        return detected if detected in SUPPORTED_LANGS else "en"

    def with_lang(translated, detected):
        return f"{translated} ({norm(detected)})"

    def real_lang(detected):
        return norm(detected)

    parts = NAME_TOKEN_RE.split(text)
    if len(parts) == 1:
        translated, detected = translate_auto(text, dst=dst)
        if not translated:
            return None, None
        return with_lang(translated, detected), real_lang(detected)

    translated_parts = []
    detected_src = None
    need_space_after_name = False
    for part in parts:
        if not part:
            continue
        if part.startswith("<") and part.endswith(">") and len(part) > 2:
            translated_parts.append(part)
            need_space_after_name = True
            continue
        if not part.strip():
            translated_parts.append(part)
            need_space_after_name = False
            continue
        piece, piece_src = translate_auto(part, dst=dst)
        if piece is None:
            return None, None
        if detected_src is None and piece_src and piece_src != "auto":
            detected_src = piece_src
        if (need_space_after_name and translated_parts and piece
                and not piece[0].isspace()
                and not translated_parts[-1].endswith((" ", "\t"))):
            translated_parts.append(" ")
        translated_parts.append(piece)
        need_space_after_name = False

    translated = "".join(translated_parts).strip()
    if not translated:
        return None, None
    if detected_src is None:
        # переводимых кусков не было (сообщение — только <Имя>) — хвост
        # «(lang)» не к чему привязывать
        return translated, None
    return with_lang(translated, detected_src), real_lang(detected_src)


# --------------------------------------------------------------------------- #
# Разбор строк лога
# --------------------------------------------------------------------------- #

def parse_chat_line(line):
    """'[19:13:45] <Uggabuga> text' -> (время, имя, текст).

    Строки без имени в скобках -> (время, None, текст) — системные сообщения.
    Не сообщение чата (пустая, 'Logging started' и т.п.) -> None.
    """
    m = RE_NAME.match(line)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = RE_COLON.match(line)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = RE_SYS.match(line)
    if m:
        return m.group(1), None, m.group(2)
    return None


def strip_log_prefix(line):
    return re.sub(r"^\[(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2}\]\s*", "",
                  line).strip()


def reassemble_lines(lines):
    """Физические строки лога -> сообщения. Строка без метки времени в
    начале — продолжение предыдущего сообщения (многострочные сообщения
    игры: стихи, объявления) — приклеивается к его телу через \\n.
    Мусор до первого сообщения, как и раньше, отбрасывается."""
    out = []
    for line in lines:
        line = line.rstrip("\r")
        if RE_TS_LINE.match(line):
            out.append(line)
        elif line.strip() and out:
            out[-1] += "\n" + line.strip()
    return out


# --------------------------------------------------------------------------- #
# Звук и уведомления (из оригинального скрипта)
# --------------------------------------------------------------------------- #

def _ensure_default_beep_wav():
    """Генерирует заметный двухтональный WAV во временной папке (если ещё нет).
    MessageBeep часто неслышен (схема звуков «Без звука»), поэтому играем
    свой синтезированный сигнал."""
    import math
    import struct
    import wave
    path = os.path.join(tempfile.gettempdir(), "wurm_chat_beep.wav")
    if os.path.isfile(path):
        return path
    rate = 22050
    duration = 0.35
    n = int(rate * duration)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            t = i / rate
            freq = 880.0 if t < duration / 2 else 1320.0
            env = min(1.0, t / 0.01, (duration - t) / 0.05)
            sample = int(12000 * env * math.sin(2 * math.pi * freq * t))
            frames += struct.pack("<h", sample)
        w.writeframes(bytes(frames))
    return path


def play_sound(sound_path=None):
    system = platform.system()
    try:
        if sound_path and os.path.isfile(sound_path):
            if system == "Windows":
                import winsound
                winsound.PlaySound(sound_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            elif system == "Darwin":
                subprocess.Popen(["afplay", sound_path])
            else:
                for player in ("paplay", "aplay", "ffplay"):
                    try:
                        cmd = [player, sound_path]
                        if player == "ffplay":
                            cmd += ["-nodisp", "-autoexit", "-loglevel", "quiet"]
                        subprocess.Popen(cmd)
                        return
                    except FileNotFoundError:
                        continue
        else:
            if system == "Windows":
                import winsound
                try:
                    winsound.PlaySound(
                        _ensure_default_beep_wav(),
                        winsound.SND_FILENAME | winsound.SND_ASYNC)
                except Exception:
                    winsound.MessageBeep(winsound.MB_ICONASTERISK)
            else:
                print("\a", end="", flush=True)
    except Exception as exc:
        log_debug(f"play_sound: {exc}")


def show_notification(title, message):
    title = xml.sax.saxutils.escape(title)
    message = xml.sax.saxutils.escape(message)
    system = platform.system()
    try:
        if system == "Windows":
            xml_str = (
                '<toast><visual><binding template="ToastGeneric">'
                f'<text>{title}</text><text>{message}</text>'
                '</binding></visual></toast>')
            # апостроф в тексте сообщения рвал PS-строку в одинарных кавычках:
            # в PS он экранируется удвоением
            xml_str = xml_str.replace("'", "''")
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
                "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null\n"
                f"$xml = '{xml_str}'\n"
                "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
                "$doc.LoadXml($xml)\n"
                "$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)\n"
                # тосты живут час — иначе копятся в центре уведомлений
                "$toast.ExpirationTime = [DateTimeOffset]::Now.AddHours(1)\n"
                # AUMID обязан быть зарегистрирован в Windows (ярлык в меню
                # «Пуск»), иначе тост молча не показывается. Свой «WurmChatGUI»
                # не зарегистрирован — отправляем от имени PowerShell.
                "$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'\n"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)\n")
            # Команда целиком в base64 (UTF-16LE): апострофы/кавычки/юникод
            # в тексте сообщения не могут сломать разбор PowerShell.
            import base64
            encoded = base64.b64encode(
                ps.encode("utf-16-le")).decode("ascii")
            creationflags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags = subprocess.CREATE_NO_WINDOW  # не мигать консолью
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-EncodedCommand", encoded],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=creationflags)
        elif system == "Darwin":
            esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(["osascript", "-e",
                              f'display notification "{esc(message)}" '
                              f'with title "{esc(title)}"'])
        else:
            try:
                subprocess.Popen(["notify-send", title, message])
            except FileNotFoundError:
                pass
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Звуки уведомлений: встроенная коллекция (папка sounds рядом с exe / в репозитории)
# --------------------------------------------------------------------------- #
BUILTIN_SOUNDS = ("beep", "chime", "bell", "ding", "pop", "alarm")


def sounds_dir():
    """Папка со встроенными звуками: в exe они распакованы рядом со скриптом
    (sys._MEIPASS), при обычном запуске — папка sounds в каталоге программы."""
    candidates = []
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None)
        if base:
            candidates.append(os.path.join(base, "sounds"))
    candidates.append(os.path.join(APP_DIR, "sounds"))
    candidates.append(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "sounds"))
    for cand in candidates:
        if cand and os.path.isdir(cand):
            return cand
    return None


def resolve_sound_path(sound_id, custom="", sdir=None):
    """Путь к wav для проигрывания: свой файл важнее встроенного;
    неизвестный/отсутствующий id — None (тогда звучит сигнал по умолчанию)."""
    if custom and os.path.isfile(custom):
        return custom
    d = sounds_dir() if sdir is None else sdir
    if d and sound_id:
        path = os.path.join(d, f"{sound_id}.wav")
        if os.path.isfile(path):
            return path
    return None


def rule_sound_path(fired_rules, channel_path=None, custom=""):
    """Звук для сработавших сигналов: первое правило со своим звуком из
    коллекции важнее звука канала; у правил без выбора — звук канала
    (channel_path), а если и его нет — сигнал по умолчанию."""
    for r in fired_rules or []:
        if r.get("sound") and r.get("sound_id") in BUILTIN_SOUNDS:
            path = resolve_sound_path(r["sound_id"], custom)
            if path:
                return path
    return channel_path


def mentioned_in(text, player_name):
    """Упоминается ли имя игрока в тексте? Границы слова, без учёта регистра."""
    pname = (player_name or "").strip()
    if not pname or not text:
        return False
    return bool(re.search(r"(?<![\w])" + re.escape(pname) + r"(?![\w])",
                          text, re.IGNORECASE))


# --------------------------------------------------------------------------- #
# Сигналы: реакция на появление подстроки в канале (звук, уведомление,
# красная подсветка вкладки)
# --------------------------------------------------------------------------- #
ALERT_THROTTLE_SECONDS = 10.0   # антиспам: правило не чаще раза в N секунд
_alert_last_fired = {}          # (channel, pattern) -> time.monotonic()


def channel_flag(value, channel):
    """Разрешён ли канал для звука/уведомлений: только если он в списке.
    Отсутствие ключа или None — ничего не разрешено (по умолчанию тихо)."""
    if not value:
        return False
    return channel in value


def set_channel_flag(cfg, key, channel, on):
    """Включить/выключить канал в списке разрешённых на звук/уведомления."""
    names = [name for name, _, _ in CHANNELS]
    cur = [c for c in (cfg.get(key) or []) if c in names]
    if on and channel not in cur:
        cur.append(channel)
    if not on and channel in cur:
        cur.remove(channel)
    cfg[key] = cur


def on_tabbar_configure(state):
    """Изменилась ширина панели вкладок — запланировать перенос вкладок
    на строки (дебаунс: не чаще раза в 50 мс)."""
    import tkinter as tk   # tkinter в модуле импортируется лениво
    width = state["bar"].winfo_width()
    if width == state.get("width"):
        return
    state["width"] = width
    job = state.get("job")
    if job is not None:
        try:
            state["bar"].after_cancel(job)
        except tk.TclError:
            pass
    state["job"] = state["bar"].after(50, lambda: layout_tab_rows(state))


def layout_tab_rows(state):
    """Жадно раскладываем вкладки (название + значки) по строкам панели:
    не поместившиеся в ширину окна уходят на строку ниже."""
    import tkinter as tk   # tkinter в модуле импортируется лениво
    state["job"] = None
    bar, groups = state["bar"], state["groups"]
    if not groups:
        return
    try:
        bar.update_idletasks()
        width = bar.winfo_width()
    except tk.TclError as exc:
        log_debug(f"tabs: layout отменён (TclError): {exc!r}")
        return
    if width < 20:   # панель ещё не отрисована — ждём следующего Configure
        if not state.get("retried"):
            state["retried"] = True
            log_debug("tabs: ширина панели <20, ждём отрисовки")
        state["job"] = bar.after(100, lambda: layout_tab_rows(state))
        return
    state["retried"] = False
    pad = 10   # 5 слева от названия + 5 справа от значков
    sizes = [b.winfo_reqwidth() + s.winfo_reqwidth()
             + n.winfo_reqwidth() + pad for b, s, n in groups]
    plan, cur, x = [], [], 0
    for gi, gw in enumerate(sizes):
        if cur and x + gw > width:
            plan.append(cur)
            cur, x = [], 0
        cur.append(gi)
        x += gw
    if cur:
        plan.append(cur)
    try:
        rows = state["rows"]
        while len(rows) < len(plan):
            rows.append(tk.Frame(bar))
        for ri, row in enumerate(rows):
            if ri < len(plan):
                if not row.winfo_ismapped():
                    row.pack(fill="x")
            else:
                row.pack_forget()
        for ri, idxs in enumerate(plan):
            row = rows[ri]
            for gi in idxs:
                b, s, n = groups[gi]
                b.pack(in_=row, side="left", padx=(5, 0))
                s.pack(in_=row, side="left")
                n.pack(in_=row, side="left", padx=(0, 5))
        # ВАЖНО: pack(in_=) не меняет ни родителя, ни порядок наложения.
        # Строки созданы ПОСЛЕ кнопок и лежат выше них в стеке панели —
        # пустые строки закрашивают кнопки. Опускаем строки вниз стека.
        for row in rows[:len(plan)]:
            row.lower()
    except tk.TclError as exc:
        log_debug(f"tabs: ОШИБКА раскладки: {exc!r}")


# --------------------------------------------------------------------------- #
# Всплывающие подсказки (hover tooltip)
# --------------------------------------------------------------------------- #
_tooltip = {"win": None, "job": None, "anchor": None}


def _tooltip_hide():
    """Скрыть и уничтожить окно подсказки, отменить отложенный показ."""
    win, job, anchor = _tooltip["win"], _tooltip["job"], _tooltip["anchor"]
    _tooltip.update(win=None, job=None, anchor=None)
    if job is not None and anchor is not None:
        try:
            anchor.after_cancel(job)
        except Exception:
            pass
    if win is not None:
        try:
            win.destroy()
        except Exception:
            pass


def _tooltip_show(widget, text):
    import tkinter as tk   # tkinter в модуле импортируется лениво
    if _tooltip["win"] is not None or not text:
        return
    win = tk.Toplevel(widget)
    win.wm_overrideredirect(True)
    win.attributes("-topmost", True)
    lbl = tk.Label(win, text=text, bg="#ffffe0", fg="#333333",
                   font=("Segoe UI", 9), padx=6, pady=3,
                   relief="solid", bd=1)
    lbl.pack()
    win.update_idletasks()
    x = widget.winfo_rootx() + 10
    y = widget.winfo_rooty() + widget.winfo_height() + 4
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    x = min(x, sw - win.winfo_width() - 8)
    y = min(y, sh - win.winfo_height() - 8)
    win.wm_geometry(f"+{max(0, x)}+{max(0, y)}")
    _tooltip["win"] = win


def widget_tooltip(widget, text_fn):
    """Подсказка при наведении. text_fn — функция без аргументов: текст
    запрашивается в момент показа, чтобы отражать текущее состояние."""
    def on_enter(_event):
        _tooltip_hide()
        _tooltip["anchor"] = widget
        _tooltip["job"] = widget.after(
            500, lambda: _tooltip_show(widget, text_fn()))

    def on_leave(_event):
        _tooltip_hide()

    widget.bind("<Enter>", on_enter)
    widget.bind("<Leave>", on_leave)


def rule_channels(rule):
    """Набор каналов правила: [] = любой. Понимает новый формат
    («channels»: список) и старый («channel»: строка)."""
    chans = rule.get("channels")
    if isinstance(chans, list):
        return [str(c) for c in chans if str(c).strip()]
    ch = str(rule.get("channel", "") or "").strip()
    return [ch] if ch else []


def match_alert_rules(rules, channel, original, translated):
    """Правила сигналов, сработавшие на сообщении.

    Каналы правила: пустой список — совпадает с любым каналом, иначе сообщение
    должно быть из одного из перечисленных. Подстрока ищется в оригинале и в
    переводе, без учёта регистра. Срабатывание антиспамится: одно правило в
    одном канале — не чаще раза в ALERT_THROTTLE_SECONDS (троттлинг по
    правилу, а не по подстроке: две одинаковые подстроки в разных правилах
    не делят между собой окно антиспама)."""
    fired = []
    haystacks = [(original or "").lower(), (translated or "").lower()]
    now = time.monotonic()
    for rule in rules or []:
        if not isinstance(rule, dict) or not rule.get("enabled", True):
            continue
        chans = rule_channels(rule)
        if chans and channel not in chans:
            continue
        pattern = str(rule.get("pattern", "") or "").strip()
        if not pattern:
            continue
        if not any(pattern.lower() in h for h in haystacks):
            continue
        key = (id(rule), channel)
        if now - _alert_last_fired.get(key, 0.0) < ALERT_THROTTLE_SECONDS:
            continue
        _alert_last_fired[key] = now
        fired.append(rule)
    # удалённые/переписанные правила оставляют мёртвые ключи — подчищаем разом
    if len(_alert_last_fired) > 200:
        for k, t in list(_alert_last_fired.items()):
            if now - t > ALERT_THROTTLE_SECONDS:
                del _alert_last_fired[k]
    return fired


# --------------------------------------------------------------------------- #
# Наблюдатель за логами (фоновый поток)
# --------------------------------------------------------------------------- #

class LogWatcher(threading.Thread):
    """Сканирует файлы каналов и кладёт новые строки в out_queue.

    Элементы: ("line", client_idx, channel_name, raw_line) — client_idx
    сопоставляет строку клиенту (вкладке) из конфигурации.
    При первом обнаружении файл позиционируется в конец (только новые
    сообщения), если start_at_end=True.
    """

    def __init__(self, logs_dir, out_queue, stop_event, client_idx=0,
                 start_at_end=True):
        super().__init__(daemon=True)
        self.logs_dir = logs_dir
        self.out = out_queue
        self.stop = stop_event
        self.client_idx = client_idx
        self.start_at_end = start_at_end
        self.offsets = {}          # путь -> позиция
        self.pending = {}          # путь -> незавершённый хвост строки
        self.mtimes = {}           # путь -> mtime последнего прочтения
        # Файлы, существовавшие при запуске, пропускаем до конца (старая
        # история не нужна). Файлы, появившиеся позже (новая переписка ЛС,
        # лог нового месяца), читаются с самого начала.
        if start_at_end:
            for name, prefix, is_pm in CHANNELS:
                for path in self.current_paths(prefix, is_pm):
                    try:
                        self.offsets[path] = os.path.getsize(path)
                    except OSError:
                        pass
        # Граница «прошлого» для кнопки «Порыться в прошлом»: снимок
        # смещений и момента запуска. Всё, что было в файлах ДО этого
        # снимка, — «прошлое»; позже появившиеся файлы сюда не входят.
        self.started_at = datetime.datetime.now()
        self.start_offsets = dict(self.offsets)

    def current_paths(self, prefix, is_pm):
        """Файлы канала: PM__<Имя>.<YYYY-MM>.txt или <канал>.<YYYY-MM>.txt."""
        if not self.logs_dir or not os.path.isdir(self.logs_dir):
            return []
        now = datetime.datetime.now()
        month = f"{now.year}-{now.month:02d}"
        if is_pm:
            import glob as globmod
            return sorted(globmod.glob(
                os.path.join(self.logs_dir, f"{prefix}*.{month}.txt")))
        path = os.path.join(self.logs_dir, f"{prefix}.{month}.txt")
        return [path] if os.path.exists(path) else []

    def run(self):
        log_debug("наблюдатель: поток запущен")
        while not self.stop.is_set():
            try:
                self._scan_once()
            except Exception as exc:    # ошибки — в очередь и в лог, поток живёт
                log_debug(f"наблюдатель: ошибка сканирования: {exc!r}")
                try:
                    self.out.put(("error", f"наблюдатель: {exc!r}"))
                except Exception:
                    pass
            self.stop.wait(0.5)
        log_debug("наблюдатель: поток остановлен")

    def _scan_once(self):
        for name, prefix, is_pm in CHANNELS:
            for path in self.current_paths(prefix, is_pm):
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if path not in self.offsets:
                    # Новый файл, появившийся во время работы (переписка
                    # ЛС, лог нового месяца) — читаем с начала.
                    self.offsets[path] = 0
                    self.pending[path] = ""
                if st.st_size < self.offsets[path]:
                    self.offsets[path] = 0   # файл перезаписан
                    self.pending[path] = ""
                if (self.mtimes.get(path) == st.st_mtime_ns
                        and st.st_size == self.offsets[path]):
                    continue                # файл не менялся — не открываем
                new_data = None
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(self.offsets[path])
                        new_data = f.read()
                        if new_data:
                            self.offsets[path] = f.tell()
                    self.mtimes[path] = st.st_mtime_ns
                except (OSError, PermissionError):
                    continue
                # Клиент игры может сбрасывать строку в файл частями: обрабатываем
                # только завершённые (\n) строки, хвост храним до следующего прохода.
                buf = self.pending.get(path, "") + (new_data or "")
                if "\n" in buf:
                    *complete, tail = buf.split("\n")
                    self.pending[path] = tail
                else:
                    complete, self.pending[path] = [], buf
                # многострочные сообщения (продолжения без метки времени)
                # собираем обратно в одно сообщение, а не роняем хвосты
                batch = [ln.strip() for ln in complete if ln.strip()]
                for msg in reassemble_lines(batch):
                    if parse_chat_line(msg):
                        self.out.put(("line", self.client_idx, name, msg))


# --------------------------------------------------------------------------- #
# Переводчик (фоновый поток)
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Жаргон торгового чата: префиксы переводим жёстко, остаток — через переводчик
# --------------------------------------------------------------------------- #

# Фиксированные переводы префиксов — по языкам перевода. Для языков вне
# таблицы префикс остаётся в оригинале (английский жаргон понятен всем).
TRADE_PREFIXES = {
    "ru": {"WTS": "Продам", "WTB": "Куплю", "PC": "Прайсчек",
           "WTT": "Обменяю"},
    "pt": {"WTS": "Vendo", "WTB": "Compro", "PC": "PC", "WTT": "Troco"},
    "de": {"WTS": "Verkaufe", "WTB": "Kaufe", "PC": "PC", "WTT": "Tausche"},
    "es": {"WTS": "Vendo", "WTB": "Compro", "PC": "PC", "WTT": "Cambio"},
    "fr": {"WTS": "Je vends", "WTB": "J'achète", "PC": "PC",
           "WTT": "J'échange"},
    "zh": {"WTS": "出售", "WTB": "求购", "PC": "询价", "WTT": "交换"},
}
# Реальный формат: «(Cad) WTS …» — серверный тег в скобках перед префиксом,
# префикс может быть в любом регистре («wts», «Wtb»).
RE_TRADE_PREFIX = re.compile(r"^(?:\(([A-Za-z]{2,10})\)\s*)?(WTS|WTB|WTT|PC)\b[:\s-]*",
                             re.IGNORECASE)
# Торговые префиксы не прячем в glossary_protect до перевода: translate_trade_body
# должен видеть их в тексте, иначе сообщение уйдёт в обычный перевод целиком
# (с токеном на месте префикса RE_TRADE_PREFIX уже не совпадёт).
TRADE_PREFIX_WORDS = {"wts", "wtb", "wtt", "pc"}


def translate_trade_body(body, dst="ru"):
    """WTS/WTB/PC в начале — фиксированный перевод, остаток — обычная цепочка.

    Возвращает (текст, исходный язык); язык — реальный код либо None."""
    m = RE_TRADE_PREFIX.match(body)
    if not m:
        return translate_preserving_names(body, dst=dst)
    prefix = m.group(2).upper()
    server = m.group(1)          # серверный тег «(Cad)», может отсутствовать
    rest = body[m.end():]
    head = TRADE_PREFIXES.get(dst, {}).get(prefix, prefix)
    if server:
        head = f"({server}) {head}"
    if not rest.strip():
        return head, "en"       # одни префикс: языковой хвост не к чему
    if dst == "ru" and re.search(r"[А-Яа-яЁё]", rest):
        tail, lang = rest, "ru"   # остаток уже на русском
    else:
        tail, detected = translate_auto(rest, dst=dst)
        if tail is None:
            return None, None
        lang = detected if detected and detected != "auto" else "und"
        if lang not in SUPPORTED_LANGS:
            lang = "en"   # шальное автоопределение — см. norm() выше
    real = lang if lang != "und" else None
    return f"{head} {tail} ({lang})", real


# --------------------------------------------------------------------------- #
# Игровой словарь: пост-обработка машинного перевода
# --------------------------------------------------------------------------- #
# Машинный перевод коверкает игровые термины Wurm («deed» -> «документ»,
# «wemp» -> «сырьё», «glimmersteel» -> «Gimmersteel», «90ql» -> «90кл»).
# Словарь исправляет перевод после машинного: файл wurm_glossary.json рядом
# с exe создаётся при первом запуске и его можно править.
#
# Формат записи:
#   "en"    — английский игровой термин: если он остался в переводе нетронутым,
#             заменяется на "ru" (по границам слова, без учёта регистра)
#   "ru"    — правильный русский вариант термина

GLOSSARY_FILE = os.path.join(APP_DIR, "wurm_glossary.json")

DEFAULT_GLOSSARY = [
    # --- Поселения и мир ---
    {"en": "deed", "ru": "поселение"},
    {"en": "guard tower", "ru": "башня стражи"},
    {"en": "spirit templar", "ru": "храмовник"},
    {"en": "guard", "ru": "страж"},
    {"en": "token", "ru": "токен"},
    {"en": "mailbox", "ru": "почтовый ящик"},
    {"en": "trader", "ru": "торговец"},
    {"en": "merchant", "ru": "торговец"},
    # --- Качество и редкость ---
    {"en": "ql", "ru": "ql"},
    {"en": "rare", "ru": "редкий"},
    {"en": "supreme", "ru": "превосходный"},
    {"en": "fantastic", "ru": "фантастический"},
    {"en": "improve", "ru": "прокачать"},
    {"en": "grind", "ru": "гринд"},
    # --- Дерево ---
    {"en": "applewood", "ru": "яблоня"},
    {"en": "birchwood", "ru": "берёза"},
    {"en": "cedarwood", "ru": "кедр"},
    {"en": "chestnutwood", "ru": "каштан"},
    {"en": "firwood", "ru": "пихта"},
    {"en": "hazelwood", "ru": "орешник"},
    {"en": "lemonwood", "ru": "лимонное дерево"},
    {"en": "lindenwood", "ru": "липа"},
    {"en": "maplewood", "ru": "клён"},
    {"en": "oakwood", "ru": "дуб"},
    {"en": "olivewood", "ru": "олива"},
    {"en": "pinewood", "ru": "сосна"},
    {"en": "raspberrywood", "ru": "малина"},
    {"en": "rosewood", "ru": "розовое дерево"},
    {"en": "walnutwood", "ru": "грецкий орех"},
    {"en": "willowwood", "ru": "ива"},
    # --- Металлы ---
    {"en": "glimmersteel", "ru": "глимерсталь"},
    {"en": "seryll", "ru": "серилл"},
    {"en": "adamantine", "ru": "адамантин"},
    {"en": "electrum", "ru": "электрум"},
    # --- Ресурсы и урожай ---
    {"en": "wemp", "ru": "вемп"},
    {"en": "plank", "ru": "доска"},
    {"en": "cotton", "ru": "хлопок"},
    {"en": "barley", "ru": "ячмень"},
    {"en": "wheat", "ru": "пшеница"},
    {"en": "nails", "ru": "гвозди"},
    {"en": "bee smoker", "ru": "окуриватель пчёл"},
    # --- Вера и магия ---
    {"en": "favour", "ru": "фавор"},
    {"en": "favor", "ru": "фавор"},
    {"en": "sermon", "ru": "проповедь"},
    {"en": "cast", "ru": "каст"},
    {"en": "courier", "ru": "курьер"},
    {"en": "rune", "ru": "руна"},
    {"en": "imbue", "ru": "имбу"},
    # --- Существа ---
    {"en": "hell hound", "ru": "адская гончая"},
    {"en": "lava spider", "ru": "лавовый паук"},
    {"en": "anaconda", "ru": "анаконда"},
    # --- Торговые префиксы в любом месте сообщения ---
    {"en": "wts", "ru": "Продам"},
    {"en": "wtb", "ru": "Куплю"},
    {"en": "wtt", "ru": "Обменяю"},
    {"en": "pc", "ru": "Прайсчек"},
    # --- Чатовые сокращения ---
    {"en": "tyvm", "ru": "большое спасибо"},
    {"en": "ty", "ru": "спасибо"},
    {"en": "thx", "ru": "спасибо"},
    {"en": "np", "ru": "не за что"},
    {"en": "nvm", "ru": "неважно"},
    {"en": "btw", "ru": "кстати"},
    {"en": "brb", "ru": "сейчас вернусь"},
    {"en": "bbl", "ru": "вернусь позже"},
    {"en": "afk", "ru": "отошёл"},
    {"en": "wb", "ru": "с возвращением"},
    {"en": "pls", "ru": "пожалуйста"},
    {"en": "plz", "ru": "пожалуйста"},
    {"en": "imo", "ru": "по-моему"},
    {"en": "imho", "ru": "по-моему"},
    {"en": "idk", "ru": "не знаю"},
    {"en": "iirc", "ru": "если не ошибаюсь"},
    {"en": "irl", "ru": "в реальной жизни"},
    {"en": "atm", "ru": "сейчас"},
    {"en": "asap", "ru": "как можно скорее"},
    {"en": "gf", "ru": "хороший бой"},
    {"en": "noob", "ru": "нуб"},
    {"en": "pm", "ru": "в личку"},
    {"en": "ea", "ru": "за штуку"},
    {"en": "each", "ru": "за штуку"},
    # --- Торговые термины ---
    {"en": "wta", "ru": "аукцион"},
    {"en": "bo", "ru": "выкуп"},
    {"en": "neg", "ru": "торг"},
    {"en": "obo", "ru": "или лучшее предложение"},
    {"en": "cod", "ru": "наложка"},
    {"en": "pickup", "ru": "самовывоз"},
    {"en": "pick up", "ru": "самовывоз"},
    {"en": "bulk", "ru": "опт"},
    # --- Игровые термины ---
    {"en": "guards", "ru": "стража"},
    {"en": "dmg", "ru": "урон"},
    {"en": "sb", "ru": "бонус сна"},
    {"en": "sp", "ru": "порошок сна"},
    {"en": "fs", "ru": "боевой навык"},
    {"en": "priest", "ru": "жрец"},
    {"en": "dirt", "ru": "земля"},
    {"en": "wagon", "ru": "фургон"},
    {"en": "large cart", "ru": "большая телега"},
    {"en": "larder", "ru": "лардер"},
    {"en": "bsb", "ru": "большой контейнер"},
    {"en": "fsb", "ru": "контейнер для еды"},
    {"en": "ccfp", "ru": "кцфп"},
    {"en": "affinity", "ru": "аффинити"},
    {"en": "unique", "ru": "уник"},
    {"en": "hota", "ru": "ХОТА"},
    {"en": "drake", "ru": "дрейк"},
    {"en": "moonmetal", "ru": "лунный металл"},
    {"en": "glim", "ru": "глим"},
    {"en": "gm", "ru": "ГМ"},
    # --- Зачарования (энчанты) ---
    {"en": "coc", "ru": "круг хитрости"},
    {"en": "woa", "ru": "ветер эпох"},
    {"en": "botd", "ru": "благословение тьмы"},
    {"en": "lt", "ru": "передача жизни"},
    {"en": "ms", "ru": "вор разума"},
    {"en": "fb", "ru": "воспламенение"},
    {"en": "fa", "ru": "метка льда"},
    {"en": "aosp", "ru": "аура разделённой боли"},
    {"en": "web armor", "ru": "паутинная броня"},
    # --- Боги ---
    {"en": "vynora", "ru": "Винора"},
    {"en": "magranon", "ru": "Магранон"},
    {"en": "libila", "ru": "Либила"},
    {"en": "fo", "ru": "Фо"},
]


def _compile_glossary(items):
    """Список записей -> (en, en_re, ru, ru_re)."""
    entries = []
    for item in items:
        if not isinstance(item, dict):
            continue
        en = str(item.get("en", "")).strip()
        ru = str(item.get("ru", "")).strip()
        if not en or not ru:
            continue
        en_re = re.compile(r"(?<![\w])" + re.escape(en) + r"(?![\w])",
                           re.IGNORECASE)
        ru_re = re.compile(r"(?<![\w])" + re.escape(ru) + r"(?![\w])",
                           re.IGNORECASE)
        entries.append((en, en_re, ru, ru_re))
    return entries


_glossary_cache = {"mtime": None, "entries": None}


def glossary_entries():
    """Скомпилированный словарь; файл перечитывается при изменении на диске —
    правки вступают в силу без перезапуска программы."""
    try:
        mtime = os.path.getmtime(GLOSSARY_FILE)
    except OSError:
        mtime = None
    if _glossary_cache["entries"] is not None and mtime == _glossary_cache["mtime"]:
        return _glossary_cache["entries"]
    items = DEFAULT_GLOSSARY
    try:
        if not os.path.exists(GLOSSARY_FILE):
            with open(GLOSSARY_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_GLOSSARY, f, ensure_ascii=False, indent=2)
        else:
            with open(GLOSSARY_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                items = loaded
    except Exception as exc:      # битый файл — работаем на встроенном словаре
        log_debug(f"словарь: не удалось прочитать {GLOSSARY_FILE}: {exc!r}")
    _glossary_cache.update(mtime=mtime, entries=_compile_glossary(items))
    return _glossary_cache["entries"]


def glossary_fix(text, dst="ru"):
    """Пост-обработка перевода игровым словарём.

    dst="ru": нетронутые английские термины -> ru-термин.
    dst="en" (панель ответа): известные ru-термины -> en, чтобы ответ вышел
    с игровой лексикой, а не дословным переводом.
    """
    if not text:
        return text
    if dst == "ru":
        for _en, en_re, ru, _ru_re in glossary_entries():
            text = en_re.sub(ru, text)
    elif dst == "en":
        for en, _en_re, ru, ru_re in glossary_entries():
            text = ru_re.sub(en, text)
    return text


# Термины, которые не прячем до перевода: их английская форма слишком
# неоднозначна (pm — ещё и «post meridiem»), поэтому для них работает только
# пост-замена оставшихся английских слов. Остальные термины словаря
# защищаются до перевода — см. glossary_protect/glossary_restore.
GLOSSARY_NO_PROTECT = {"pm"}

_glossary_sorted = {"mtime": None, "entries": None}


def _glossary_sorted_entries():
    """Записи словаря, отсортированные по длине en (длинные — первыми:
    «guard tower» должен матчиться раньше, чем «guard»).

    Кэш живёт ровно столько, сколько основной (по mtime файла): внешняя
    правка wurm_glossary.json подхватывается без перезапуска программы."""
    entries = glossary_entries()
    try:
        mtime = os.path.getmtime(GLOSSARY_FILE)
    except OSError:
        mtime = None
    if _glossary_sorted["entries"] is not None \
            and mtime == _glossary_sorted["mtime"]:
        return _glossary_sorted["entries"]
    ordered = sorted(entries, key=lambda e: -len(e[0]))
    _glossary_sorted["mtime"] = mtime
    _glossary_sorted["entries"] = ordered
    return ordered


def glossary_protect(text, dst="ru"):
    """Прячет известные en-термины в токены ДО перевода.

    Google иначе переводил термины наугад («nails» -> «ногти»), и пост-замена
    уже не могла ничего исправить — английского слова в тексте не оставалось.
    Возвращает (текст, [(токен, ru), ...]) — restore вернёт термины после
    перевода. Работает только для ru: словарь en<->ru.
    """
    terms = []
    if dst != "ru" or not text:
        return text, terms
    n = 0
    for en, en_re, ru, _ru_re in _glossary_sorted_entries():
        if en.lower() in GLOSSARY_NO_PROTECT or en.lower() in TRADE_PREFIX_WORDS:
            continue
        if en_re.search(text):
            token = f"<Gl{n}>"
            text = en_re.sub(token, text)
            terms.append((token, ru))
        n += 1
    return text, terms


def glossary_restore(text, terms):
    """Возвращает на место токены glossary_protect (термины в финальном виде).

    Машинный перевод иногда съедает пробелы вокруг токена и склеивает слова
    («изумруд<Gl57>»), поэтому при подстановке проверяем соседей: если термин
    оказался вплотную к букве, цифре или скобке — добавляем пробел.
    """
    if not text or not terms:
        return text

    def _make_repl(ru):
        def repl(m):
            s = m.string
            start, end = m.span()
            pre = s[start - 1] if start > 0 else " "
            post = s[end] if end < len(s) else " "
            res = ru
            if (pre.isalnum() or pre in ")]}") and ru[0].isalnum():
                res = " " + res
            if post.isalnum() or post in "([{":
                res = res + " "
            return res
        return repl

    for token, ru in terms:
        # регистр токена машинный перевод иногда меняет — ищем нечувствительно
        text = re.sub(re.escape(token), _make_repl(ru), text,
                      flags=re.IGNORECASE)
    return text


def glossary_protect_ru(text):
    """Обратная защита для панели ответа (ru → en): известные ru-термины
    прячутся в токены ДО перевода, после — на место встаёт en-термин.

    Без этого Google переводил термины наугад («вемп» -> «hemp») и
    пост-замена glossary_fix(en) уже не могла ничего исправить. Возвращает
    (текст, [(токен, en), ...]) — восстановление общее, glossary_restore."""
    terms = []
    if not text:
        return text, terms
    n = 0
    for en, _en_re, ru, ru_re in _glossary_sorted_entries():
        if ru_re.search(text):
            token = f"<Gl{n}>"
            text = ru_re.sub(token, text)
            terms.append((token, en))
        n += 1
    return text, terms


def glossary_raw_items():
    """Сырой список записей словаря (dict'ы) из файла или встроенный."""
    try:
        if os.path.exists(GLOSSARY_FILE):
            with open(GLOSSARY_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                return [x for x in loaded if isinstance(x, dict)]
    except Exception as exc:
        log_debug(f"словарь: ошибка чтения для редактора: {exc!r}")
    return [dict(x) for x in DEFAULT_GLOSSARY]


def glossary_save_items(items):
    """Сохранить записи в файл и сбросить кэш — изменения действуют сразу."""
    with open(GLOSSARY_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    _glossary_cache.update(mtime=None, entries=None)
    _glossary_sorted.update(mtime=None, entries=None)


# --------------------------------------------------------------------------- #
# Игровая валюта: 1g = 100s = 10000c = 1000000i (золото/серебро/медь/железо)
# --------------------------------------------------------------------------- #
# Суммы вроде «20s» или «2g 50s 30c 40i» машинный перевод коверкает, поэтому
# до перевода они прячутся в токены <CurN> (механизм защиты имён), а после —
# подставляются словами на целевом языке.
CURRENCY_WORDS = {
    # единица: {язык: (именит. ед., родит. ед.)} — для ru две формы,
    # для остальных языков склонения нет
    "g": {"ru": ("золото", "золота"), "en": ("gold",), "de": ("Gold",),
          "es": ("oro",), "fr": ("or",), "pt": ("ouro",), "zh": ("金",)},
    "s": {"ru": ("серебро", "серебра"), "en": ("silver",), "de": ("Silber",),
          "es": ("plata",), "fr": ("argent",), "pt": ("prata",), "zh": ("银",)},
    "c": {"ru": ("медь", "меди"), "en": ("copper",), "de": ("Kupfer",),
          "es": ("cobre",), "fr": ("cuivre",), "pt": ("cobre",), "zh": ("铜",)},
    "i": {"ru": ("железо", "железа"), "en": ("iron",), "de": ("Eisen",),
          "es": ("hierro",), "fr": ("fer",), "pt": ("ferro",), "zh": ("铁",)},
}

RE_CURRENCY = re.compile(r"(?<![\w])(\d+)\s*([gsci])(?![\w])", re.IGNORECASE)


def currency_protect(text):
    """'WTS sword 2g 50s' -> ('WTS sword <Cur0> <Cur1>', [('2','g'),('50','s')])."""
    amounts = []

    def rep(m):
        amounts.append((m.group(1), m.group(2).lower()))
        return f"<Cur{len(amounts) - 1}>"

    return RE_CURRENCY.sub(rep, text), amounts


def currency_restore(text, amounts, dst):
    """Токены <CurN> -> '20 серебра' (ru), '20 silver' (en) и т.д.

    Соседние суммы разделяет запятой («2g 50s» -> «2 золота, 50 серебра»),
    перед токеном/после него восстанавливает пробел, если переводчик его «съел»
    (Google склеивает «меч <Cur0>» в «меч<Cur0>»)."""
    if not text or not amounts:
        return text

    def word_for(num, unit):
        if dst is None:                    # опция выключена — «20s» как есть
            return unit
        forms = CURRENCY_WORDS.get(unit, {}).get(dst) or CURRENCY_WORDS[unit]["en"]
        if dst == "ru" and len(forms) > 1:
            # 1 серебро, но 20 серебра («11 серебра» — тоже родительный)
            n = int(num) % 100
            return forms[0] if n % 10 == 1 and n != 11 else forms[1]
        return forms[0]

    def replace(m, num, word, raw):
        full = m.string
        before = full[m.start() - 1] if m.start() > 0 else " "
        after = full[m.end()] if m.end() < len(full) else " "
        out = f"{num}{word}" if raw else f"{num} {word}"
        if before.isalnum() or before in ">)]»":
            out = " " + out
        if after.isalnum() or after in "<([«":
            out += " "
        return out

    # сначала схлопываем разделители между соседними сумками в запятую
    for i in range(1, len(amounts)):
        text = re.sub(rf"<Cur{i - 1}>\s*<Cur{i}>",
                      f"<Cur{i - 1}>, <Cur{i}>", text)
    raw_mode = dst is None
    for i, (num, unit) in enumerate(amounts):
        text = re.sub(rf"<Cur{i}>",
                      lambda m, n=num, u=unit: replace(m, n, word_for(n, u),
                                                       raw_mode),
                      text, count=1)
    return text


def preview_translation(body, target_lang, use_currency=True):
    """Тот же конвейер перевода, что и для чатового сообщения (TranslateWorker),
    но одной фразой: валюта и термины словаря прячутся в токены, машинный
    перевод, пост-обработка словарём и восстановление. Для окна проверки
    фразы в редакторе словаря. Возвращает (текст, исходный язык) или
    (None, None) при сбое перевода."""
    if not body:
        return body, None
    trade_prefixed = bool(RE_TRADE_PREFIX.match(body))
    if target_lang == "ru" and re.search(r"[А-Яа-яЁё]", body) \
            and not trade_prefixed:
        return body, "ru"            # уже на языке перевода — как в чате
    body_t, cur_amounts = currency_protect(body)
    body_t, gl_terms = glossary_protect(body_t, target_lang)
    if trade_prefixed:
        translated, src_lang = translate_trade_body(body_t, target_lang)
    else:
        translated, src_lang = translate_preserving_names(body_t, target_lang)
    if translated is None:
        return None, None
    if target_lang == "ru":
        translated = glossary_fix(translated, "ru")
    translated = currency_restore(translated, cur_amounts,
                                  target_lang if use_currency else None)
    translated = glossary_restore(translated, gl_terms)
    return translated, src_lang


def open_glossary_editor(parent, target_lang="ru"):
    """Ненавязчивый редактор словаря: правка прямо в ячейке двойным щелчком,
    каждое изменение сразу пишется в wurm_glossary.json и действует без
    перезапуска. Открывается по клику на ссылке «Словарь» в углу окна."""
    import tkinter as tk
    from tkinter import ttk

    win = tk.Toplevel(parent)
    win.title(f"{tr('glossary_title')} — {tr('app_title')}")
    win.geometry("820x440")
    win.minsize(640, 320)
    win.transient(parent)

    items = glossary_raw_items()

    # ---------- таблица ----------
    table_frame = ttk.Frame(win)
    table_frame.pack(fill="both", expand=True, padx=8, pady=(8, 4))
    cols = ("en", "ru")
    table = ttk.Treeview(table_frame, columns=cols, show="headings", height=12)
    table.heading("en", text=tr("col_en"))
    table.heading("ru", text=tr("col_ru"))
    table.column("en", width=260, anchor="w")
    table.column("ru", width=260, anchor="w")
    scroll = ttk.Scrollbar(table_frame, orient="vertical", command=table.yview)
    table.configure(yscrollcommand=scroll.set)
    table.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    status_var = tk.StringVar()

    def refresh():
        table.delete(*table.get_children())
        for i, it in enumerate(items):
            table.insert("", "end", iid=str(i), values=(
                it.get("en", ""), it.get("ru", "")))

    def autosave():
        try:
            glossary_save_items(items)
            status_var.set(tr("glossary_status", n=len(items)))
        except Exception as exc:
            status_var.set(tr("glossary_save_err", exc=exc))
            log_debug(f"словарь: ошибка сохранения: {exc!r}")

    # ---------- правка прямо в ячейке ----------
    edit_state = {"widget": None, "iid": None, "col": None}

    def commit_edit(_e=None):
        """Завершить правку ячейки: записать значение и сохранить."""
        wdg = edit_state["widget"]
        if wdg is None:
            return
        value = wdg.get().strip()
        iid, col = edit_state["iid"], edit_state["col"]
        edit_state["widget"] = None
        wdg.destroy()
        if iid is None:
            return
        i = int(iid)
        if i >= len(items):
            return
        it = items[i]
        it[col] = value
        # пустая запись (без термина и перевода) — удаляем
        if not it.get("en") and not it.get("ru"):
            items.pop(i)
            refresh()
        else:
            table.item(iid, values=(it.get("en", ""), it.get("ru", "")))
        autosave()

    def cancel_edit(_e=None):
        wdg = edit_state["widget"]
        edit_state["widget"] = None
        edit_state["iid"] = None
        if wdg is not None:
            wdg.destroy()

    def start_edit(event):
        if edit_state["widget"] is not None:
            commit_edit()
        iid = table.identify_row(event.y)
        col_id = table.identify_column(event.x)   # '#1', '#2', '#3'
        if not iid or not col_id:
            return
        col = cols[int(col_id[1:]) - 1]
        bbox = table.bbox(iid, col)
        if not bbox:
            return
        x, y, w, h = bbox
        i = int(iid)
        old = items[i].get(col, "")
        entry = ttk.Entry(table_frame)
        entry.place(x=x, y=y, width=w, height=h)
        entry.insert(0, old)
        entry.select_range(0, "end")
        entry.focus_set()
        edit_state.update(widget=entry, iid=iid, col=col)
        entry.bind("<Return>", commit_edit)
        entry.bind("<FocusOut>", commit_edit)
        entry.bind("<Escape>", cancel_edit)

    table.bind("<Double-Button-1>", start_edit)

    # ---------- кнопки ----------
    btns = ttk.Frame(win)
    btns.pack(fill="x", padx=8, pady=2)

    def add_row():
        if edit_state["widget"] is not None:
            commit_edit()
        items.append({"en": "", "ru": ""})
        refresh()
        iid = str(len(items) - 1)
        table.see(iid)
        table.selection_set(iid)
        # сразу начинаем править термин
        bbox = table.bbox(iid, "en")
        if bbox:
            x, y, w, h = bbox
            entry = ttk.Entry(table_frame)
            entry.place(x=x, y=y, width=w, height=h)
            entry.focus_set()
            edit_state.update(widget=entry, iid=iid, col="en")
            entry.bind("<Return>", commit_edit)
            entry.bind("<FocusOut>", commit_edit)
            entry.bind("<Escape>", cancel_edit)

    def delete_selected():
        if edit_state["widget"] is not None:
            cancel_edit()
        sel = table.selection()
        if not sel:
            return
        for s in sorted((int(x) for x in sel), reverse=True):
            items.pop(s)
        refresh()
        autosave()

    ttk.Button(btns, text=tr("btn_add_row"), command=add_row).pack(side="left")
    ttk.Button(btns, text=tr("btn_delete_sel"), command=delete_selected).pack(
        side="left", padx=4)
    ttk.Button(btns, text=tr("btn_open_file"),
               command=lambda: os.startfile(GLOSSARY_FILE)).pack(side="left", padx=8)

    # ---------- низ: проверка фразы ----------
    test = ttk.LabelFrame(win, text=tr("test_frame"))
    test.pack(fill="x", padx=8, pady=(4, 4))
    test_var = tk.StringVar()
    test_entry = ttk.Entry(test, textvariable=test_var)
    test_entry.pack(side="left", fill="x", expand=True, padx=(6, 4), pady=4)
    test_out = tk.StringVar(value="—")
    ttk.Label(test, textvariable=test_out, foreground="#005a9e",
              wraplength=280).pack(side="left", padx=(0, 6))

    test_job = {"id": 0}
    test_q = queue.Queue()

    def on_test(*_a):
        test_job["id"] += 1
        text = test_var.get().strip()
        if not text:
            test_out.set("—")
            return
        test_out.set("…")
        job = test_job["id"]
        # антидребезг: переводим после короткой паузы после последней буквы
        win.after(600, lambda: run_test(job, text))

    def run_test(job, text):
        if job != test_job["id"]:
            return              # текст уже изменился
        def work():
            try:
                res, _src = preview_translation(text, target_lang)
            except Exception as exc:
                res = None
                log_debug(f"словарь: проверка фразы: {exc!r}")
            test_q.put((job, res))
        threading.Thread(target=work, daemon=True).start()

    def poll_test():
        try:
            while True:
                job, res = test_q.get_nowait()
                if job == test_job["id"]:
                    test_out.set(res if res else tr("status_translate_fail"))
        except queue.Empty:
            pass
        try:
            win.after(100, poll_test)
        except tk.TclError:
            pass                  # окно уже закрыто

    test_var.trace_add("write", on_test)
    poll_test()

    ttk.Label(win, textvariable=status_var, foreground="#666666").pack(
        anchor="w", padx=10, pady=(0, 6))

    refresh()
    autosave()   # статус про автосохранение + гарантия, что файл на месте
    test_entry.focus_set()


class TranslateWorker(threading.Thread):
    """Берёт ("line", канал, строка), отдаёт ("done", канал, время, имя,
    оригинал, перевод|None). Переводит только тело сообщения, имя — нет."""

    def __init__(self, raw_queue, done_queue, stop_event, enabled=True,
                 target_lang="ru", options=None):
        super().__init__(daemon=True)
        self.raw = raw_queue
        self.done = done_queue
        self.stop = stop_event
        self.enabled = enabled
        self.target_lang = target_lang
        self.options = options if options is not None else {}
        self.cache = {}
        self._last_req = 0.0

    def run(self):
        log_debug("переводчик: поток запущен")
        while not self.stop.is_set():
            try:
                self._process_one()
            except Exception as exc:
                # Любая неожиданная ошибка — в лог, поток продолжает работу.
                log_debug(f"переводчик: неожиданная ошибка: {exc!r}\n"
                          + traceback.format_exc())
                self.stop.wait(0.5)
        log_debug("переводчик: поток остановлен")

    def _process_one(self):
        try:
            item = self.raw.get(timeout=0.2)
        except queue.Empty:
            return
        if not item or item[0] != "line":
            return
        _, client_idx, channel, line = item[:4]
        # 5-й элемент — токен «прошлого» (кнопка «Порыться в прошлом»):
        # результат вернётся как past_done и будет выведен иначе — раскраской
        # «прошлого». Токен (а не просто флаг) отсекает опоздавшие результаты
        # предыдущей раскопки, если кнопку нажали повторно
        past_tok = item[4] if len(item) > 4 else None

        parsed = parse_chat_line(line)
        if parsed:
            t, name, body = parsed
        else:  # нестандартная строка — покажем как есть
            t, name, body = "", "", strip_log_prefix(line)
        if not body:
            return  # пустые строки вида "[17:33:11] "

        translated = None
        src_lang = None
        cur_amounts = []
        opts = self.options
        # канал явно выключен из перевода (Настройки / ПКМ по вкладке);
        # национальные чаты («Russian GL») лежат в этом списке по умолчанию
        keep_as_is = channel in (opts.get("no_translate_channels") or [])
        use_currency = bool(opts.get("translate_currency", True))
        if self.enabled and body and not keep_as_is:
            trade_prefixed = channel == "Trade" and bool(RE_TRADE_PREFIX.match(body))
            if (self.target_lang == "ru" and re.search(r"[А-Яа-яЁё]", body)
                    and not trade_prefixed):
                translated = body  # уже на русском — перевод не нужен
                src_lang = "ru"
            elif body in self.cache:
                translated, src_lang = self.cache[body]   # в кэше — уже финальный вид
            else:
                pause = TRANSLATE_INTERVAL - (time.monotonic() - self._last_req)
                if pause > 0:
                    time.sleep(pause)
                # валюта прячется в токены до перевода, вернётся после
                # (даже при выключенной опции — чтобы «20s» дошло до экрана
                # в исходном виде, а не перекошенным машинным переводом)
                body_t, cur_amounts = currency_protect(body)
                # термины словаря тоже прячем до перевода, чтобы Google
                # не переводил их наугад («nails» -> «ногти»)
                body_t, gl_terms = glossary_protect(body_t, self.target_lang)
                if trade_prefixed:
                    translated, src_lang = translate_trade_body(body_t,
                                                                self.target_lang)
                else:
                    translated, src_lang = translate_preserving_names(
                        body_t, self.target_lang)
                self._last_req = time.monotonic()
                if translated is not None:
                    # Пост-обработка словарём до кэша: кэш хранит финальный вид.
                    if self.target_lang == "ru":
                        translated = glossary_fix(translated, "ru")
                    translated = currency_restore(
                        translated, cur_amounts,
                        self.target_lang if use_currency else None)
                    translated = glossary_restore(translated, gl_terms)
                    self.cache[body] = (translated, src_lang)
                    if len(self.cache) > TRANSLATE_CACHE_MAX:
                        self.cache.pop(next(iter(self.cache)))
        elif keep_as_is:
            translated = body   # языковой канал с выключенным переводом — как есть
        # сообщение «уже на русском»: валюта всё равно оформляется словами
        if (translated is not None and cur_amounts
                and body not in self.cache):
            translated = currency_restore(translated, cur_amounts,
                                          self.target_lang if use_currency
                                          else None)

        if past_tok is not None:
            self.done.put(("past_done", past_tok, client_idx, channel, t,
                           name, body, translated, src_lang))
        else:
            self.done.put(("done", client_idx, channel, t, name, body,
                           translated, src_lang))


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #

DEFAULTS = {
    "clients": [],           # список {"name", "path"} — по клиенту на копию игры
    "translate": True,
    "on_top": False,
    "show_original": False,
    "sound_file": "",       # свой wav-файл (если задан — важнее встроенных)
    "sound": "beep",        # встроенный звук: id из BUILTIN_SOUNDS
    "channel_sounds": {},   # звук по каналам: {"Trade": "bell", …} ; пусто = общий
    "target_lang": "auto",   # auto → определить по языку клиента Steam
    "reply_src": "",         # "" → как основной язык перевода; иначе явный код
    "reply_dst": "en",       # панель ответа: на какой язык переводить
    # национальные чаты («Russian GL») выключены из перевода по умолчанию
    # (механизм тот же, что у других каналов; см. миграцию в load_config)
    "no_translate_channels": [],  # каналы БЕЗ перевода (галочка 🌐)
    "translate_currency": True,        # «20s» → «20 серебра»; выкл — как есть
    "auto_copy_reply": True,           # перевод ответа — сразу в буфер обмена
    "sound_channels": [],   # каналы со звуком на новые сообщения (иконка 🔊)
    "notify_channels": [],  # каналы с уведомлениями на новые сообщения (🔔)
    "auto_update": True,    # автоустановка обновлений: спросить, скачать,
                            # заменить, перезапустить (вкл. по умолчанию)
    "alerts": [],                      # сигналы: channels/pattern/enabled/...
}
# Сигнал по умолчанию: фраза археологии «можно копать центр бывшего дида»
# приходит в канал Event — реагируем всеми способами. У уже существующих
# установок список добавится один раз: как только alerts впервые окажется в
# файле конфигурации, дальше списком управляет пользователь.
# channels — список наблюдаемых каналов; пустой список = любой канал.
DEFAULTS["alerts"] = [{
    "channels": ["Event"],
    "pattern": "complete the location details",
    "note": "обнаружен центр поселения с сундучком, иди ищи и копай",
    "enabled": True, "sound": True, "notify": True}]


def load_config():
    raw = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
    # Миграция: общий флаг translate_lang_channels (до 1.7.7.6) уступил место
    # поканальному выключению перевода. Был False или ключа нет — национальные
    # чаты («Russian GL») выключены; был True — пользователь явно включал
    # перевод, уважаем это. Решение фиксируем один раз: после первого
    # сохранения новой версии старый ключ исчезает, новый — есть, и
    # миграция больше не трогает список (пользователь может править сам).
    if "no_translate_channels" not in raw or "translate_lang_channels" in raw:
        lang_names = [n for n, _p, _pm in CHANNELS
                      if RE_LANG_CHANNEL.match(n)]
        off = list(cfg.get("no_translate_channels") or [])
        if raw.get("translate_lang_channels") is True:
            off = [c for c in off if c not in lang_names]
        else:
            for c in lang_names:
                if c not in off:
                    off.append(c)
        cfg["no_translate_channels"] = off
    # миграция с версий < 1.5.0: единый путь logs_dir -> список клиентов
    if not cfg["clients"] and raw.get("logs_dir"):
        path = raw["logs_dir"]
        name = os.path.basename(os.path.dirname(path)) or "Клиент"
        cfg["clients"] = [{"name": name, "path": path}]
        save_config(cfg)
    # Путь, который указал пользователь, — священен: используем как есть,
    # даже если папка сейчас недоступна (другой диск, переименование).
    # Молчаливая подмена приводила к потере пути при каждом запуске.
    # Автопоиск (Steam + автономный клиент) работает только по кнопке «Найти».
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Локализация интерфейса
# --------------------------------------------------------------------------- #
# Язык интерфейса = язык перевода чата. «auto» — определить по языку клиента
# Steam (реестр). Названия языков в списке выбора — на родных языках: даже
# если случайно выбрать непонятный, свой всегда можно найти в списке.
UI_LANG = "ru"   # действующий язык интерфейса; задаётся в начале run_gui

# (код, название на родном языке) — порядок = порядок в списке выбора
LANGUAGES = [
    ("auto", "Auto"),
    ("ru", "Русский"),
    ("en", "English"),
    ("pt", "Português"),
    ("de", "Deutsch"),
    ("es", "Español"),
    ("fr", "Français"),
    ("zh", "中文"),
]
SUPPORTED_LANGS = [code for code, _ in LANGUAGES if code != "auto"]

# Язык Steam (значение реестра) -> код интерфейса
STEAM_LANG_MAP = {
    "russian": "ru", "english": "en", "portuguese": "pt",
    "german": "de", "spanish": "es", "french": "fr",
    "schinese": "zh", "tchinese": "zh",
}

# Названия целевых языков в речи каждого языка интерфейса
# («Перевод на русский» / «Translate to Russian» / «Traduzir para russo»…)
LANG_OBJ = {
    "ru": {"ru": "русский", "en": "английский", "pt": "португальский",
           "de": "немецкий", "es": "испанский", "fr": "французский",
           "zh": "китайский"},
    "en": {"ru": "Russian", "en": "English", "pt": "Portuguese",
           "de": "German", "es": "Spanish", "fr": "French", "zh": "Chinese"},
    "pt": {"ru": "russo", "en": "inglês", "pt": "português",
           "de": "alemão", "es": "espanhol", "fr": "francês", "zh": "chinês"},
    "de": {"ru": "Russisch", "en": "Englisch", "pt": "Portugiesisch",
           "de": "Deutsch", "es": "Spanisch", "fr": "Französisch",
           "zh": "Chinesisch"},
    "es": {"ru": "ruso", "en": "inglés", "pt": "portugués",
           "de": "alemán", "es": "español", "fr": "francés", "zh": "chino"},
    "fr": {"ru": "russe", "en": "anglais", "pt": "portugais",
           "de": "allemand", "es": "espagnol", "fr": "français",
           "zh": "chinois"},
    "zh": {"ru": "俄语", "en": "英语", "pt": "葡萄牙语",
           "de": "德语", "es": "西班牙语", "fr": "法语", "zh": "中文"},
}

I18N = {
    "ru": {
        "app_title": "Wurm Chat — переводчик",
        "clients_label": "Клиенты:",
        "clients_btn": "Клиенты…",
        "btn_settings": "Настройки…",
        "alerts_btn": "Сигналы…",
        "alerts_title": "Сигналы на сообщения",
        "alerts_col_on": "Вкл",
        "alerts_col_channel": "Канал",
        "alerts_col_pattern": "Подстрока",
        "alerts_col_sound": "Звук",
        "alerts_col_notify": "Уведомл.",
        "alerts_col_note": "Текст уведомл.",
        "alert_any": "Любой",
        "alerts_channel": "Канал:",
        "alerts_pattern": "Подстрока:",
        "alerts_on": "Вкл",
        "alert_sound": "Звук",
        "alert_notify": "Уведомление",
        "alerts_add": "Новый",
        "alerts_save_rule": "Сохранить",
        "alerts_remove": "Удалить",
        "alerts_done": "Готово",
        "alerts_hint": "Срабатывает при появлении подстроки в канале "
                       "(проверяются оригинал и перевод, без учёта регистра). "
                       "Вкладка подсвечивается красным; одно правило не "
                       "срабатывает чаще раза в 10 секунд. Щелчок по ✓/— "
                       "переключает флажок, по каналу открывает список выбора "
                       "каналов, щелчок по «Звук» — выбор сигнала из коллекции "
                       "или «Выкл.», двойной щелчок по подстроке или тексту "
                       "уведомления — правка. Свой текст в уведомлении: "
                       "показывается вместо текста сообщения.",
        "alert_toast_title": "Сигнал",
        "lastalert_title": "Последнее оповещение",
        "lastalert_jump": "К сообщению",
        "settings_title": "Настройки перевода",
        "opt_lang_channels": "Переводить языковые чаты (Russian GL, English GL, …)",
        "opt_currency": "Переводить денежные единицы (20s → 20 серебра)",
        "opt_auto_copy": "Автоматически копировать перевод ответа в буфер обмена",
        "settings_hint": "Изменения применяются сразу, перезапуск не нужен.",
        "settings_close": "Закрыть",
        "clients_none": "не заданы",
        "clients_edit_hint": "Выберите клиента в списке — имя и путь "
                               "можно править ниже.",
        "btn_apply": "Применить",
        "btn_browse": "Обзор…",
        "pick_title": "Выбор папки логов",
        "pick_up": "Вверх",
        "pick_choose": "Выбрать эту папку",
        "pick_status_ok": "Файлов логов: {n} ({files})",
        "pick_status_none": "Файлов логов нет — это не папка logs игры",
        "pick_status_bad": "Папку прочитать нельзя",
        "pick_sel_preview": "В «{name}»",
        "lang_label": "Язык:",
        "chk_translate": "Перевод на {lang}",
        "chk_sound": "Звук",
        "chk_notify": "Уведомления",
        "chk_original": "Оригинал",
        "chk_on_top": "Поверх окон",
        "tab_menu_translate": "Переводить этот канал",
        "tab_menu_sound": "Звук из этого канала",
        "tab_menu_notify": "Уведомления из этого канала",
        "tab_tt_sound_on": "Звук на новые сообщения: вкл (переключить — правая кнопка)",
        "tab_tt_sound_off": "Звук на новые сообщения: выкл (переключить — правая кнопка)",
        "tab_tt_notify_on": "Всплывающие уведомления: вкл (переключить — правая кнопка)",
        "tab_tt_notify_off": "Всплывающие уведомления: выкл (переключить — правая кнопка)",
        "reply_label": "Ответ:",
        "ctx_reply": "Ответить",
        "btn_start": "▶ Старт",
        "btn_stop": "■ Стоп",
        "btn_clear": "Очистить всё",
        "btn_search": "🔍 Найти",
        "btn_dig": "🕑 Прошлое",
        "dig_title": "Порыться в прошлом",
        "dig_target": "Клиент: {ch}",
        "dig_mode_count": "последних сообщений",
        "dig_mode_minutes": "минут до запуска",
        "dig_label_count": "Сколько сообщений взять:",
        "dig_label_minutes": "За сколько минут до запуска:",
        "dig_pos": "Куда вывести:",
        "dig_pos_start": "в начало текущего чата",
        "dig_pos_end": "в конец текущего чата",
        "dig_show": "Показать",
        "dig_cancel": "Отмена",
        "dig_need_run": "Сначала запустите слежение — «прошлое» "
                        "появляется только после его запуска.",
        "dig_empty": "До запуска в этот канал никто не писал.",
        "dig_header_count": "⏳ Прошлое — последние {n} сообщений",
        "dig_header_minutes": "⏳ Прошлое — за {n} мин до запуска",
        "dig_hint": "Перевод прошлых сообщений идёт построчно и не "
                        "мгновен: ориентировочно секунда на сообщение.",
        "glossary_link": "Словарь",
        "no_clients_hint": "Клиенты не заданы. Нажмите «Клиенты…» и добавьте "
                           "папку логов …\\players\\<Игрок>\\logs",
        "status_stopped": "Остановлено",
        "status_add_client": "Добавьте хотя бы одного клиента: кнопка «Клиенты…»",
        "status_bad_path": "Папка логов недоступна: {path} — проверьте в «Клиенты…»",
        "status_watching": "Слежу за {n} клиентом(-ами): {names}. {mode}",
        "status_mode_tail": "только новые сообщения (старые пропущены)",
        "status_running": "Работает. Поймано сообщений: {n}. {backend}",
        "status_copied": "Скопировано в буфер: {text}",
        "status_translate_fail": "(не удалось перевести)",
        "backend_gtx": "перевод: Google ✓",
        "backend_mymemory": "перевод: запасной сервис",
        "backend_fallback": "перевод: Google в перерыве, запасной сервис",
        "backend_unknown": "перевод: нет данных",
        "watcher_restarted": "Наблюдатель перезапущен после сбоя (см. debug-лог)",
        "worker_restarted": "Переводчик перезапущен после сбоя (см. debug-лог)",
        "msg_untranslated": "(не удалось перевести)",
        "search_title": "Поиск: {query} — найдено: {n}",
        "search_empty": "Ничего не найдено.",
        "search_hint": "Двойной клик по результату — перейти ко вкладке",
        "tray_show": "Показать",
        "tray_quit": "Выход",
        "opt_auto_update": "Автоматически скачивать и устанавливать обновления",
        "update_dl_title": "Обновление переводчика",
        "update_ask_title": "Доступна новая версия",
        "update_ask_msg": "Вышла версия {new} (у вас {cur}).\n"
                          "Скачать и установить?",
        "btn_check_updates": "Проверить обновления",
        "update_none": "У вас последняя версия ({cur}).",
        "update_check_failed": "Не удалось проверить обновления.\n"
                               "Проверьте подключение к сети.",
        "update_download_failed": "Не удалось скачать обновление. \n"
                               "Проверьте подключение к сети и повторите.",
        "update_ask_no_remind": "Не напоминать об этом обновлении",
        "update_yes": "Да",
        "update_no": "Нет",
        "update_downloading": "Скачивание версии v{ver}…\n"
                             "Окно закроется само, переводчик перезапустится.",
        "update_restarting": "Обновление скачано. Перезапуск…",
        "update_failed": "Не удалось скачать обновление.\n"
                         "Открыть страницу загрузки?",
        "update_msg": "Доступна новая версия v{new} (у вас v{cur}).\n\n"
                      "Открыть страницу загрузки?\n"
                      "(Скачанный exe можно положить поверх старого — "
                      "настройки сохранятся)",
        "instance_msg": "Приложение уже запущено.\n\n"
                        "Да — активировать уже запущенное\n"
                        "Нет — закрыть его и запустить новое\n"
                        "Отмена — ничего не делать",
        "dlg_clients_title": "Клиенты — папки логов",
        "col_name": "Имя",
        "col_path": "Папка логов",
        "btn_find_all": "Найти всех",
        "btn_add": "Добавить…",
        "btn_remove": "Удалить",
        "btn_close": "Закрыть",
        "client_default": "Клиент {n}",
        "find_none": "Папки логов не найдены ни в Steam, ни в автономном клиенте.",
        "find_all_present": "Все найденные папки уже в списке.",
        "add_dir_title": "Папка логов — …\\<Игрок>\\logs",
        "kind_steam": "Steam",
        "kind_site": "сайт",
        "glossary_title": "Словарь терминов",
        "col_en": "Англ. термин",
        "col_ru": "Перевод",
        "btn_add_row": "➕ Добавить",
        "btn_delete_sel": "Удалить выбранное",
        "btn_open_file": "Открыть файл…",
        "test_frame": "Перевести фразу (как в чате)",
        "glossary_status": "Записей: {n} — каждое изменение сохраняется сразу "
                           "и действует без перезапуска",
        "glossary_save_err": "Ошибка сохранения: {exc}",
        "err_output": "Ошибка вывода: {exc}",
        "opt_sound": "Звук уведомлений:",
        "opt_channels": "Каналы: перевод, звук, уведомления:",
        "opt_ch_translate": "перевод",
        "opt_ch_sound": "звук",
        "opt_ch_notify": "уведомления",
        "opt_channels_hint": "То же самое даёт правая кнопка мыши по вкладке канала.",
        "btn_play": "Прослушать",
        "tab_menu_sound_pick": "Звук этого канала",
        "sound_default": "Как общий",
        "sound_channel_default": "Как у канала",
        "sound_off": "Выкл.",
        "alerts_log_btn": "Журнал",
        "alerts_log_title": "Журнал срабатываний сигналов",
        "alerts_log_empty": "Срабатываний пока не было.",
        "alerts_log_col_time": "Время",
        "alerts_log_col_channel": "Канал",
        "alerts_log_col_text": "Текст",
        "snd_beep": "Стандартный",
        "snd_chime": "Колокольчик",
        "snd_bell": "Колокол",
        "snd_ding": "Дзынь",
        "snd_pop": "Поп",
        "snd_alarm": "Тревога",
    },
    "en": {
        "app_title": "Wurm Chat Translator",
        "clients_label": "Clients:",
        "clients_btn": "Clients…",
        "btn_settings": "Settings…",
        "alerts_btn": "Alerts…",
        "alerts_title": "Message alerts",
        "alerts_col_on": "On",
        "alerts_col_channel": "Channel",
        "alerts_col_pattern": "Substring",
        "alerts_col_sound": "Sound",
        "alerts_col_notify": "Notify",
        "alerts_col_note": "Notify text",
        "alert_any": "Any",
        "alerts_channel": "Channel:",
        "alerts_pattern": "Substring:",
        "alerts_on": "On",
        "alert_sound": "Sound",
        "alert_notify": "Notification",
        "alerts_add": "New",
        "alerts_save_rule": "Save",
        "alerts_remove": "Delete",
        "alerts_done": "Done",
        "alerts_hint": "Fires when the substring appears in the channel "
                       "(original and translation are checked, "
                       "case-insensitive). The tab turns red; one rule fires "
                       "at most once per 10 seconds. Click ✓/— to toggle, "
                       "click the channel to open a channel checklist, "
                       "click Sound to pick a signal (or turn it off), "
                       "double-click the substring to edit.",
        "alert_toast_title": "Alert",
        "lastalert_title": "Last alert",
        "lastalert_jump": "Jump to message",
        "settings_title": "Translation settings",
        "opt_lang_channels": "Translate language channels (Russian GL, English GL, …)",
        "opt_auto_copy": "Automatically copy the reply translation to the clipboard",
        "opt_currency": "Translate currency units (20s → 20 silver)",
        "settings_hint": "Changes apply immediately, no restart needed.",
        "settings_close": "Close",
        "clients_none": "not set",
        "clients_edit_hint": "Select a client in the list — name and "
                               "path can be edited below.",
        "btn_apply": "Apply",
        "btn_browse": "Browse…",
        "pick_title": "Choose the logs folder",
        "pick_up": "Up",
        "pick_choose": "Select this folder",
        "pick_status_ok": "Log files: {n} ({files})",
        "pick_status_none": "No log files — this is not the game's logs folder",
        "pick_status_bad": "This folder cannot be read",
        "pick_sel_preview": "In \"{name}\"",
        "lang_label": "Language:",
        "chk_translate": "Translate to {lang}",
        "chk_sound": "Sound",
        "chk_notify": "Notifications",
        "chk_original": "Original",
        "chk_on_top": "Always on top",
        "tab_menu_translate": "Translate this channel",
        "tab_menu_sound": "Sound from this channel",
        "tab_menu_notify": "Notifications from this channel",
        "tab_tt_sound_on": "Sound on new messages: on (right-click to toggle)",
        "tab_tt_sound_off": "Sound on new messages: off (right-click to toggle)",
        "tab_tt_notify_on": "Popup notifications: on (right-click to toggle)",
        "tab_tt_notify_off": "Popup notifications: off (right-click to toggle)",
        "reply_label": "Reply:",
        "ctx_reply": "Reply",
        "btn_start": "▶ Start",
        "btn_stop": "■ Stop",
        "btn_clear": "Clear all",
        "btn_search": "🔍 Find",
        "btn_dig": "🕑 Past",
        "dig_title": "Dig into the past",
        "dig_target": "Client: {ch}",
        "dig_mode_count": "last messages",
        "dig_mode_minutes": "minutes before startup",
        "dig_label_count": "How many messages to take:",
        "dig_label_minutes": "How many minutes before startup:",
        "dig_pos": "Where to put them:",
        "dig_pos_start": "at the top of the chat",
        "dig_pos_end": "at the bottom of the chat",
        "dig_show": "Show",
        "dig_cancel": "Cancel",
        "dig_need_run": "Start watching first — the “past” only exists "
                        "after it starts.",
        "dig_empty": "Nobody wrote to this channel before startup.",
        "dig_header_count": "⏳ Past — last {n} messages",
        "dig_header_minutes": "⏳ Past — {n} min before startup",
        "dig_hint": "Translating past messages takes a while — roughly one "
                        "second per message.",
        "glossary_link": "Glossary",
        "no_clients_hint": "No clients set. Click “Clients…” and add a logs "
                           "folder …\\players\\<Player>\\logs",
        "status_stopped": "Stopped",
        "status_add_client": "Add at least one client: the “Clients…” button",
        "status_bad_path": "Logs folder unavailable: {path} — check in “Clients…”",
        "status_watching": "Watching {n} client(s): {names}. {mode}",
        "status_mode_tail": "new messages only (old ones skipped)",
        "status_running": "Running. Messages caught: {n}. {backend}",
        "status_copied": "Copied to clipboard: {text}",
        "status_translate_fail": "(translation failed)",
        "backend_gtx": "translation: Google ✓",
        "backend_mymemory": "translation: fallback service",
        "backend_fallback": "translation: Google cooling down, fallback service",
        "backend_unknown": "translation: no data",
        "watcher_restarted": "Watcher restarted after a crash (see debug log)",
        "worker_restarted": "Translator restarted after a crash (see debug log)",
        "msg_untranslated": "(failed to translate)",
        "search_title": "Search: {query} — found: {n}",
        "search_empty": "Nothing found.",
        "search_hint": "Double-click a result to jump to its tab",
        "tray_show": "Show",
        "tray_quit": "Exit",
        "opt_auto_update": "Automatically download and install updates",
        "update_dl_title": "Translator update",
        "update_ask_title": "Update available",
        "update_ask_msg": "Version {new} is available (you have {cur}).\n"
                          "Download and install it now?",
        "btn_check_updates": "Check for updates",
        "update_none": "You have the latest version ({cur}).",
        "update_check_failed": "Couldn't check for updates.\n"
                               "Check your internet connection.",
        "update_download_failed": "Could not download the update. \n"
                               "Check your internet connection and try again.",
        "update_ask_no_remind": "Don't remind me about this update",
        "update_yes": "Yes",
        "update_no": "No",
        "update_downloading": "Downloading v{ver}…\n"
                             "This window will close itself and the translator "
                             "will restart.",
        "update_restarting": "Update downloaded. Restarting…",
        "update_failed": "Could not download the update.\n"
                         "Open the download page?",
        "update_msg": "A new version v{new} is available (you have v{cur}).\n\n"
                      "Open the download page?\n"
                      "(You can place the downloaded exe over the old one — "
                      "settings are kept)",
        "instance_msg": "The application is already running.\n\n"
                        "Yes — activate the running one\n"
                        "No — close it and start a new one\n"
                        "Cancel — do nothing",
        "dlg_clients_title": "Clients — log folders",
        "col_name": "Name",
        "col_path": "Log folder",
        "btn_find_all": "Find all",
        "btn_add": "Add…",
        "btn_remove": "Remove",
        "btn_close": "Close",
        "client_default": "Client {n}",
        "find_none": "No log folders found in Steam or the standalone client.",
        "find_all_present": "All found folders are already in the list.",
        "add_dir_title": "Log folder — …\\<Player>\\logs",
        "kind_steam": "Steam",
        "kind_site": "site",
        "glossary_title": "Game glossary",
        "col_en": "EN term",
        "col_ru": "Translation",
        "btn_add_row": "➕ Add",
        "btn_delete_sel": "Delete selected",
        "btn_open_file": "Open file…",
        "test_frame": "Translate a phrase (as in chat)",
        "glossary_status": "Entries: {n} — every change is saved immediately "
                           "and applies without a restart",
        "glossary_save_err": "Save error: {exc}",
        "err_output": "Output error: {exc}",
        "opt_sound": "Notification sound:",
        "opt_channels": "Channels: translation, sound, notifications:",
        "opt_ch_translate": "translate",
        "opt_ch_sound": "sound",
        "opt_ch_notify": "notifications",
        "opt_channels_hint": "Right-clicking a channel tab does the same.",
        "btn_play": "Play",
        "tab_menu_sound_pick": "Sound for this channel",
        "sound_default": "Same as global",
        "sound_channel_default": "Channel default",
        "sound_off": "Off",
        "alerts_log_btn": "Log",
        "alerts_log_title": "Alert history",
        "alerts_log_empty": "No alerts yet.",
        "alerts_log_col_time": "Time",
        "alerts_log_col_channel": "Channel",
        "alerts_log_col_text": "Text",
        "snd_beep": "Default",
        "snd_chime": "Chime",
        "snd_bell": "Bell",
        "snd_ding": "Ding",
        "snd_pop": "Pop",
        "snd_alarm": "Alarm",
    },
    "pt": {
        "app_title": "Wurm Chat — Tradutor",
        "clients_label": "Clientes:",
        "clients_btn": "Clientes…",
        "btn_settings": "Configurações…",
        "alerts_btn": "Alertas…",
        "alerts_title": "Alertas de mensagens",
        "alerts_col_on": "Ativo",
        "alerts_col_channel": "Canal",
        "alerts_col_pattern": "Subcadeia",
        "alerts_col_sound": "Som",
        "alerts_col_notify": "Notif.",
        "alerts_col_note": "Texto notif.",
        "alert_any": "Qualquer",
        "alerts_channel": "Canal:",
        "alerts_pattern": "Subcadeia:",
        "alerts_on": "Ativo",
        "alert_sound": "Som",
        "alert_notify": "Notificação",
        "alerts_add": "Novo",
        "alerts_save_rule": "Guardar",
        "alerts_remove": "Remover",
        "alerts_done": "Concluir",
        "alerts_hint": "Dispara quando a subcadeia aparece no canal (original "
                       "e tradução, sem distinção de maiúsculas). A aba fica "
                       "vermelha; uma regra dispara no máximo uma vez por "
                       "10 segundos. Clique em ✓/— para alternar, no canal "
                       "para abrir a lista de canais; clique em «Som» para "
                       "escolher um sinal da coleção (ou desligar); duplo "
                       "clique na subcadeia para editar.",
        "alert_toast_title": "Alerta",
        "lastalert_title": "Último alerta",
        "lastalert_jump": "Ir para a mensagem",
        "settings_title": "Configurações de tradução",
        "opt_auto_copy": "Copiar automaticamente a tradução da resposta para a área de transferência",
        "opt_lang_channels": "Traduzir chats de idiomas (Russian GL, English GL, …)",
        "opt_currency": "Traduzir unidades monetárias (20s → 20 prata)",
        "settings_hint": "As alterações são aplicadas imediatamente, sem reiniciar.",
        "settings_close": "Fechar",
        "clients_none": "não definidos",
        "clients_edit_hint": "Selecione um cliente na lista — nome e "
                               "pasta podem ser editados abaixo.",
        "btn_apply": "Aplicar",
        "btn_browse": "Procurar…",
        "pick_title": "Escolher a pasta de logs",
        "pick_up": "Subir",
        "pick_choose": "Selecionar esta pasta",
        "pick_status_ok": "Arquivos de log: {n} ({files})",
        "pick_status_none": "Sem arquivos de log — esta não é a pasta de logs do jogo",
        "pick_status_bad": "Não foi possível ler a pasta",
        "pick_sel_preview": "Em «{name}»",
        "lang_label": "Idioma:",
        "chk_translate": "Traduzir para {lang}",
        "chk_sound": "Som",
        "chk_notify": "Notificações",
        "chk_original": "Original",
        "chk_on_top": "Sempre visível",
        "tab_menu_translate": "Traduzir este canal",
        "tab_menu_sound": "Som deste canal",
        "tab_menu_notify": "Notificações deste canal",
        "tab_tt_sound_on": "Som em novas mensagens: ativado (botão direito para alternar)",
        "tab_tt_sound_off": "Som em novas mensagens: desativado (botão direito para alternar)",
        "tab_tt_notify_on": "Notificações pop-up: ativadas (botão direito para alternar)",
        "tab_tt_notify_off": "Notificações pop-up: desativadas (botão direito para alternar)",
        "reply_label": "Responder:",
        "ctx_reply": "Responder",
        "btn_start": "▶ Iniciar",
        "btn_stop": "■ Parar",
        "btn_clear": "Limpar tudo",
        "btn_search": "🔍 Buscar",
        "btn_dig": "🕑 Passado",
        "dig_title": "Cavar o passado",
        "dig_target": "Cliente: {ch}",
        "dig_mode_count": "últimas mensagens",
        "dig_mode_minutes": "minutos antes do início",
        "dig_label_count": "Quantas mensagens pegar:",
        "dig_label_minutes": "Quantos minutos antes do início:",
        "dig_pos": "Onde colocar:",
        "dig_pos_start": "no início do chat",
        "dig_pos_end": "no final do chat",
        "dig_show": "Mostrar",
        "dig_cancel": "Cancelar",
        "dig_need_run": "Primeiro inicie a vigilância — o «passado» só "
                        "existe depois dela.",
        "dig_empty": "Ninguém escreveu neste canal antes do início.",
        "dig_header_count": "⏳ Passado — últimas {n} mensagens",
        "dig_header_minutes": "⏳ Passado — {n} min antes do início",
        "dig_hint": "Traduzir mensagens antigas leva um tempo — cerca de um "
                        "segundo por mensagem.",
        "glossary_link": "Glossário",
        "no_clients_hint": "Sem clientes. Clique em «Clientes…» e adicione a "
                           "pasta de logs …\\players\\<Jogador>\\logs",
        "status_stopped": "Parado",
        "status_add_client": "Adicione pelo menos um cliente: botão «Clientes…»",
        "status_bad_path": "Pasta de logs indisponível: {path} — verifique em "
                           "«Clientes…»",
        "status_watching": "Observando {n} cliente(s): {names}. {mode}",
        "status_mode_tail": "apenas mensagens novas (antigas ignoradas)",
        "status_running": "Funcionando. Mensagens capturadas: {n}. {backend}",
        "status_copied": "Copiado para a área de transferência: {text}",
        "status_translate_fail": "(falha ao traduzir)",
        "backend_gtx": "tradução: Google ✓",
        "backend_mymemory": "tradução: serviço reserva",
        "backend_fallback": "tradução: Google em pausa, serviço reserva",
        "backend_unknown": "tradução: sem dados",
        "watcher_restarted": "Observador reiniciado após falha (veja o log)",
        "worker_restarted": "Tradutor reiniciado após falha (veja o log)",
        "msg_untranslated": "(falha ao traduzir)",
        "search_title": "Busca: {query} — encontrados: {n}",
        "search_empty": "Nada encontrado.",
        "search_hint": "Duplo clique num resultado para ir à aba",
        "tray_show": "Mostrar",
        "tray_quit": "Sair",
        "opt_auto_update": "Baixar e instalar atualizações automaticamente",
        "update_dl_title": "Atualização do tradutor",
        "update_ask_title": "Nova versão disponível",
        "update_ask_msg": "Versão {new} disponível (você tem {cur}).\n"
                          "Baixar e instalar agora?",
        "btn_check_updates": "Verificar atualizações",
        "update_none": "Você tem a versão mais recente ({cur}).",
        "update_check_failed": "Não foi possível verificar atualizações.\n"
                               "Verifique a conexão.",
        "update_download_failed": "Não foi possível baixar a atualização. \n"
                               "Verifique a conexão e tente novamente.",
        "update_ask_no_remind": "Não lembrar desta atualização",
        "update_yes": "Sim",
        "update_no": "Não",
        "update_downloading": "Baixando a versão v{ver}…\n"
                             "Esta janela se fechará sozinha e o tradutor "
                             "reiniciará.",
        "update_restarting": "Atualização baixada. Reiniciando…",
        "update_failed": "Não foi possível baixar a atualização.\n"
                         "Abrir a página de download?",
        "update_msg": "Nova versão v{new} disponível (você tem v{cur}).\n\n"
                      "Abrir a página de download?\n"
                      "(O exe baixado pode substituir o antigo — as "
                      "configurações ficam)",
        "instance_msg": "O aplicativo já está em execução.\n\n"
                        "Sim — ativar o em execução\n"
                        "Não — fechá-lo e iniciar um novo\n"
                        "Cancelar — não fazer nada",
        "dlg_clients_title": "Clientes — pastas de logs",
        "col_name": "Nome",
        "col_path": "Pasta de logs",
        "btn_find_all": "Encontrar todos",
        "btn_add": "Adicionar…",
        "btn_remove": "Remover",
        "btn_close": "Fechar",
        "client_default": "Cliente {n}",
        "find_none": "Pastas de logs não encontradas no Steam nem no cliente "
                     "autônomo.",
        "find_all_present": "Todas as pastas encontradas já estão na lista.",
        "add_dir_title": "Pasta de logs — …\\<Jogador>\\logs",
        "kind_steam": "Steam",
        "kind_site": "site",
        "glossary_title": "Glossário de termos",
        "col_en": "Termo em inglês",
        "col_ru": "Tradução",
        "btn_add_row": "➕ Adicionar",
        "btn_delete_sel": "Excluir selecionados",
        "btn_open_file": "Abrir arquivo…",
        "test_frame": "Traduzir frase (como no chat)",
        "glossary_status": "Entradas: {n} — cada alteração é salva imediatamente "
                           "e vale sem reiniciar",
        "glossary_save_err": "Erro ao salvar: {exc}",
        "err_output": "Erro de saída: {exc}",
        "opt_sound": "Som de notificação:",
        "opt_channels": "Canais: tradução, som, notificações:",
        "opt_ch_translate": "traduzir",
        "opt_ch_sound": "som",
        "opt_ch_notify": "notificações",
        "opt_channels_hint": "O botão direito sobre a aba do canal faz o mesmo.",
        "btn_play": "Ouvir",
        "tab_menu_sound_pick": "Som deste canal",
        "sound_default": "Como o geral",
        "sound_channel_default": "Som do canal",
        "sound_off": "Desligado",
        "alerts_log_btn": "Registro",
        "alerts_log_title": "Histórico de alertas",
        "alerts_log_empty": "Nenhum alerta ainda.",
        "alerts_log_col_time": "Hora",
        "alerts_log_col_channel": "Canal",
        "alerts_log_col_text": "Texto",
        "snd_beep": "Padrão",
        "snd_chime": "Carrilhão",
        "snd_bell": "Sino",
        "snd_ding": "Ding",
        "snd_pop": "Pop",
        "snd_alarm": "Alarme",
    },
    "de": {
        "app_title": "Wurm Chat — Übersetzer",
        "clients_label": "Clients:",
        "clients_btn": "Clients…",
        "btn_settings": "Einstellungen…",
        "alerts_btn": "Signale…",
        "alerts_title": "Nachrichtensignale",
        "alerts_col_on": "An",
        "alerts_col_channel": "Kanal",
        "alerts_col_pattern": "Teilstring",
        "alerts_col_sound": "Ton",
        "alerts_col_notify": "Benachr.",
        "alerts_col_note": "Benachr.-Text",
        "alert_any": "Jeder",
        "alerts_channel": "Kanal:",
        "alerts_pattern": "Teilstring:",
        "alerts_on": "An",
        "alert_sound": "Ton",
        "alert_notify": "Benachrichtigung",
        "alerts_add": "Neu",
        "alerts_save_rule": "Speichern",
        "alerts_remove": "Entfernen",
        "alerts_done": "Fertig",
        "alerts_hint": "Löst aus, wenn der Teilstring im Kanal erscheint "
                       "(Original und Übersetzung werden geprüft, "
                       "Groß-/Kleinschreibung egal). Der Reiter wird rot; "
                       "eine Regel löst höchstens einmal pro 10 Sekunden aus. "
                       "Klick auf ✓/— schaltet um, auf den Kanal öffnet die "
                       "Kanalauswahl, Klick auf «Ton» wählt ein Signal aus der "
                       "Sound-Sammlung (oder schaltet ab), Doppelklick auf "
                       "den Teilstring bearbeitet ihn.",
        "alert_toast_title": "Signal",
        "lastalert_title": "Letzter Alarm",
        "lastalert_jump": "Zur Nachricht",
        "opt_auto_copy": "Übersetzte Antwort automatisch in die Zwischenablage kopieren",
        "settings_title": "Übersetzungseinstellungen",
        "opt_lang_channels": "Sprach-Chats übersetzen (Russian GL, English GL, …)",
        "opt_currency": "Währungseinheiten übersetzen (20s → 20 Silber)",
        "settings_hint": "Änderungen werden sofort übernommen, Neustart nicht erforderlich.",
        "settings_close": "Schließen",
        "clients_none": "nicht gesetzt",
        "clients_edit_hint": "Wählen Sie einen Client in der Liste — "
                               "Name und Ordner können unten bearbeitet "
                               "werden.",
        "btn_apply": "Übernehmen",
        "btn_browse": "Durchsuchen…",
        "pick_title": "Log-Ordner wählen",
        "pick_up": "Nach oben",
        "pick_choose": "Diesen Ordner wählen",
        "pick_status_ok": "Log-Dateien: {n} ({files})",
        "pick_status_none": "Keine Log-Dateien — das ist nicht der Log-Ordner des Spiels",
        "pick_status_bad": "Ordner kann nicht gelesen werden",
        "pick_sel_preview": "In „{name}“",
        "lang_label": "Sprache:",
        "chk_translate": "Übersetzen auf {lang}",
        "chk_sound": "Ton",
        "chk_notify": "Benachrichtigungen",
        "chk_original": "Original",
        "chk_on_top": "Immer im Vordergrund",
        "tab_menu_translate": "Diesen Kanal übersetzen",
        "tab_menu_sound": "Ton aus diesem Kanal",
        "tab_menu_notify": "Benachrichtigungen aus diesem Kanal",
        "tab_tt_sound_on": "Ton bei neuen Nachrichten: an (Rechtsklick zum Umschalten)",
        "tab_tt_sound_off": "Ton bei neuen Nachrichten: aus (Rechtsklick zum Umschalten)",
        "tab_tt_notify_on": "Pop-up-Benachrichtigungen: an (Rechtsklick zum Umschalten)",
        "tab_tt_notify_off": "Pop-up-Benachrichtigungen: aus (Rechtsklick zum Umschalten)",
        "reply_label": "Antwort:",
        "ctx_reply": "Antworten",
        "btn_start": "▶ Start",
        "btn_stop": "■ Stopp",
        "btn_clear": "Alles löschen",
        "btn_search": "🔍 Suchen",
        "btn_dig": "🕑 Vergangenheit",
        "dig_title": "In der Vergangenheit wühlen",
        "dig_target": "Client: {ch}",
        "dig_mode_count": "letzte Nachrichten",
        "dig_mode_minutes": "Minuten vor dem Start",
        "dig_label_count": "Wie viele Nachrichten nehmen:",
        "dig_label_minutes": "Wie viele Minuten vor dem Start:",
        "dig_pos": "Wohin damit:",
        "dig_pos_start": "an den Anfang des Chats",
        "dig_pos_end": "an das Ende des Chats",
        "dig_show": "Anzeigen",
        "dig_cancel": "Abbrechen",
        "dig_need_run": "Erst die Überwachung starten — «Vergangenheit» "
                        "gibt es erst danach.",
        "dig_empty": "Vor dem Start hat niemand in diesen Kanal "
                     "geschrieben.",
        "dig_header_count": "⏳ Vergangenheit — letzte {n} Nachrichten",
        "dig_header_minutes": "⏳ Vergangenheit — {n} Min. vor dem Start",
        "dig_hint": "Das Übersetzen alter Nachrichten dauert — ungefähr eine "
                        "Sekunde pro Nachricht.",
        "glossary_link": "Glossar",
        "no_clients_hint": "Keine Clients gesetzt. Klicken Sie auf „Clients…” "
                           "und fügen Sie einen Log-Ordner hinzu: "
                           "…\\players\\<Spieler>\\logs",
        "status_stopped": "Gestoppt",
        "status_add_client": "Mindestens einen Client hinzufügen: Schaltfläche "
                             "„Clients…”",
        "status_bad_path": "Log-Ordner nicht verfügbar: {path} — prüfen unter "
                           "„Clients…”",
        "status_watching": "Beobachte {n} Client(s): {names}. {mode}",
        "status_mode_tail": "nur neue Nachrichten (alte übersprungen)",
        "status_running": "Läuft. Nachrichten erfasst: {n}. {backend}",
        "status_copied": "In die Zwischenablage kopiert: {text}",
        "status_translate_fail": "(Übersetzung fehlgeschlagen)",
        "backend_gtx": "Übersetzung: Google ✓",
        "backend_mymemory": "Übersetzung: Ersatzdienst",
        "backend_fallback": "Übersetzung: Google pausiert, Ersatzdienst",
        "backend_unknown": "Übersetzung: keine Daten",
        "watcher_restarted": "Beobachter nach Absturz neu gestartet (s. Debug-Log)",
        "worker_restarted": "Übersetzer nach Absturz neu gestartet (s. Debug-Log)",
        "msg_untranslated": "(Übersetzung fehlgeschlagen)",
        "search_title": "Suche: {query} — gefunden: {n}",
        "search_empty": "Nichts gefunden.",
        "search_hint": "Doppelklick auf ein Ergebnis springt zum Tab",
        "tray_show": "Anzeigen",
        "tray_quit": "Beenden",
        "opt_auto_update": "Updates automatisch herunterladen und installieren",
        "update_dl_title": "Update des Übersetzers",
        "update_ask_title": "Neue Version verfügbar",
        "update_ask_msg": "Version {new} ist verfügbar (installiert: {cur}).\n"
                          "Jetzt herunterladen und installieren?",
        "btn_check_updates": "Nach Updates suchen",
        "update_none": "Sie haben die neueste Version ({cur}).",
        "update_check_failed": "Update-Prüfung fehlgeschlagen.\n"
                               "Verbindung prüfen.",
        "update_download_failed": "Das Update konnte nicht heruntergeladen werden. \n"
                               "Verbindung prüfen und erneut versuchen.",
        "update_ask_no_remind": "Nicht mehr an dieses Update erinnern",
        "update_yes": "Ja",
        "update_no": "Nein",
        "update_downloading": "Version v{ver} wird heruntergeladen…\n"
                             "Dieses Fenster schließt sich von selbst, der "
                             "Übersetzer startet neu.",
        "update_restarting": "Update heruntergeladen. Neustart…",
        "update_failed": "Das Update konnte nicht heruntergeladen werden.\n"
                         "Download-Seite öffnen?",
        "update_msg": "Neue Version v{new} verfügbar (Sie haben v{cur}).\n\n"
                      "Download-Seite öffnen?\n"
                      "(Die heruntergeladene exe kann über die alte gelegt "
                      "werden — Einstellungen bleiben erhalten)",
        "instance_msg": "Die Anwendung läuft bereits.\n\n"
                        "Ja — laufende aktivieren\n"
                        "Nein — beenden und neue starten\n"
                        "Abbrechen — nichts tun",
        "dlg_clients_title": "Clients — Log-Ordner",
        "col_name": "Name",
        "col_path": "Log-Ordner",
        "btn_find_all": "Alle finden",
        "btn_add": "Hinzufügen…",
        "btn_remove": "Entfernen",
        "btn_close": "Schließen",
        "client_default": "Client {n}",
        "find_none": "Keine Log-Ordner in Steam oder dem Standalone-Client "
                     "gefunden.",
        "find_all_present": "Alle gefundenen Ordner sind bereits in der Liste.",
        "add_dir_title": "Log-Ordner — …\\<Spieler>\\logs",
        "kind_steam": "Steam",
        "kind_site": "Standalone",
        "glossary_title": "Glossar der Begriffe",
        "col_en": "EN-Begriff",
        "col_ru": "Übersetzung",
        "btn_add_row": "➕ Hinzufügen",
        "btn_delete_sel": "Ausgewählte löschen",
        "btn_open_file": "Datei öffnen…",
        "test_frame": "Phrase übersetzen (wie im Chat)",
        "glossary_status": "Einträge: {n} — jede Änderung wird sofort "
                           "gespeichert und ohne Neustart wirksam",
        "glossary_save_err": "Speicherfehler: {exc}",
        "err_output": "Ausgabefehler: {exc}",
        "opt_sound": "Benachrichtigungston:",
        "opt_channels": "Kanäle: Übersetzung, Ton, Benachrichtigungen:",
        "opt_ch_translate": "übersetzen",
        "opt_ch_sound": "Ton",
        "opt_ch_notify": "Benachrichtigungen",
        "opt_channels_hint": "Ein Rechtsklick auf den Kanalreiter tut dasselbe.",
        "btn_play": "Anhören",
        "tab_menu_sound_pick": "Ton für diesen Kanal",
        "sound_default": "Wie global",
        "sound_channel_default": "Kanal-Standard",
        "sound_off": "Aus",
        "alerts_log_btn": "Protokoll",
        "alerts_log_title": "Signalprotokoll",
        "alerts_log_empty": "Noch keine Signale.",
        "alerts_log_col_time": "Zeit",
        "alerts_log_col_channel": "Kanal",
        "alerts_log_col_text": "Text",
        "snd_beep": "Standard",
        "snd_chime": "Glockenspiel",
        "snd_bell": "Glocke",
        "snd_ding": "Ding",
        "snd_pop": "Plopp",
        "snd_alarm": "Alarm",
    },
    "es": {
        "app_title": "Wurm Chat — Traductor",
        "clients_label": "Clientes:",
        "clients_btn": "Clientes…",
        "opt_auto_copy": "Copiar automáticamente la traducción de la respuesta al portapapeles",
        "btn_settings": "Ajustes…",
        "alerts_btn": "Alertas…",
        "alerts_title": "Alertas de mensajes",
        "alerts_col_on": "Activo",
        "alerts_col_channel": "Canal",
        "alerts_col_pattern": "Subcadena",
        "alerts_col_sound": "Sonido",
        "alerts_col_notify": "Notif.",
        "alerts_col_note": "Texto de notif.",
        "alert_any": "Cualquiera",
        "alerts_channel": "Canal:",
        "alerts_pattern": "Subcadena:",
        "alerts_on": "Activo",
        "alert_sound": "Sonido",
        "alert_notify": "Notificación",
        "alerts_add": "Nuevo",
        "alerts_save_rule": "Guardar",
        "alerts_remove": "Eliminar",
        "alerts_done": "Listo",
        "alerts_hint": "Se activa cuando la subcadena aparece en el canal (se "
                       "comprueban el original y la traducción, sin distinguir "
                       "mayúsculas). La pestaña se pone roja; una regla se "
                       "activa como máximo una vez cada 10 segundos. Clic en "
                       "✓/— para alternar, en el canal para abrir la lista de "
                       "canales; clic en «Sonido» para elegir una señal de la "
                       "colección (o apagarla); doble clic en la subcadena "
                       "para editar.",
        "alert_toast_title": "Alerta",
        "lastalert_title": "Última alerta",
        "lastalert_jump": "Ir al mensaje",
        "settings_title": "Ajustes de traducción",
        "opt_lang_channels": "Traducir chats de idiomas (Russian GL, English GL, …)",
        "opt_currency": "Traducir unidades monetarias (20s → 20 plata)",
        "settings_hint": "Los cambios se aplican inmediatamente, sin reiniciar.",
        "settings_close": "Cerrar",
        "clients_none": "sin definir",
        "clients_edit_hint": "Seleccione un cliente en la lista — el "
                               "nombre y la carpeta se pueden editar "
                               "abajo.",
        "btn_apply": "Aplicar",
        "btn_browse": "Examinar…",
        "pick_title": "Elegir la carpeta de logs",
        "pick_up": "Subir",
        "pick_choose": "Elegir esta carpeta",
        "pick_status_ok": "Archivos de registro: {n} ({files})",
        "pick_status_none": "Sin archivos de registro: esta no es la carpeta de logs del juego",
        "pick_status_bad": "No se puede leer la carpeta",
        "pick_sel_preview": "En «{name}»",
        "lang_label": "Idioma:",
        "chk_translate": "Traducir a {lang}",
        "chk_sound": "Sonido",
        "chk_notify": "Notificaciones",
        "chk_original": "Original",
        "chk_on_top": "Siempre visible",
        "tab_menu_translate": "Traducir este canal",
        "tab_menu_sound": "Sonido de este canal",
        "tab_menu_notify": "Notificaciones de este canal",
        "tab_tt_sound_on": "Sonido en mensajes nuevos: activado (clic derecho para cambiar)",
        "tab_tt_sound_off": "Sonido en mensajes nuevos: desactivado (clic derecho para cambiar)",
        "tab_tt_notify_on": "Notificaciones emergentes: activadas (clic derecho para cambiar)",
        "tab_tt_notify_off": "Notificaciones emergentes: desactivadas (clic derecho para cambiar)",
        "reply_label": "Responder:",
        "ctx_reply": "Responder",
        "btn_start": "▶ Iniciar",
        "btn_stop": "■ Detener",
        "btn_clear": "Borrar todo",
        "btn_search": "🔍 Buscar",
        "btn_dig": "🕑 Pasado",
        "dig_title": "Hurgar en el pasado",
        "dig_target": "Cliente: {ch}",
        "dig_mode_count": "últimos mensajes",
        "dig_mode_minutes": "minutos antes del inicio",
        "dig_label_count": "Cuántos mensajes tomar:",
        "dig_label_minutes": "Cuántos minutos antes del inicio:",
        "dig_pos": "Dónde ponerlos:",
        "dig_pos_start": "al inicio del chat",
        "dig_pos_end": "al final del chat",
        "dig_show": "Mostrar",
        "dig_cancel": "Cancelar",
        "dig_need_run": "Primero inicia el seguimiento: el «pasado» solo "
                        "existe después de eso.",
        "dig_empty": "Nadie escribió en este canal antes del inicio.",
        "dig_header_count": "⏳ Pasado — últimos {n} mensajes",
        "dig_header_minutes": "⏳ Pasado — {n} min antes del inicio",
        "dig_hint": "Traducir mensajes antiguos lleva un tiempo: aproximadamente "
                        "un segundo por mensaje.",
        "glossary_link": "Glosario",
        "no_clients_hint": "Sin clientes. Pulse «Clientes…» y añada la carpeta "
                           "de logs …\\players\\<Jugador>\\logs",
        "status_stopped": "Detenido",
        "status_add_client": "Añada al menos un cliente: botón «Clientes…»",
        "status_bad_path": "Carpeta de logs no disponible: {path} — compruébela "
                           "en «Clientes…»",
        "status_watching": "Vigilando {n} cliente(s): {names}. {mode}",
        "status_mode_tail": "solo mensajes nuevos (los antiguos se omiten)",
        "status_running": "Funcionando. Mensajes capturados: {n}. {backend}",
        "status_copied": "Copiado al portapapeles: {text}",
        "status_translate_fail": "(no se pudo traducir)",
        "backend_gtx": "traducción: Google ✓",
        "backend_mymemory": "traducción: servicio de respaldo",
        "backend_fallback": "traducción: Google en pausa, servicio de respaldo",
        "backend_unknown": "traducción: sin datos",
        "watcher_restarted": "Observador reiniciado tras un fallo (ver log)",
        "worker_restarted": "Traductor reiniciado tras un fallo (ver log)",
        "msg_untranslated": "(no se pudo traducir)",
        "search_title": "Búsqueda: {query} — encontrados: {n}",
        "search_empty": "No se encontró nada.",
        "search_hint": "Doble clic en un resultado para ir a su pestaña",
        "tray_show": "Mostrar",
        "tray_quit": "Salir",
        "opt_auto_update": "Descargar e instalar actualizaciones "
                          "automáticamente",
        "update_dl_title": "Actualización del traductor",
        "update_ask_title": "Nueva versión disponible",
        "update_ask_msg": "Versión {new} disponible (tienes {cur}).\n"
                          "¿Descargar e instalar ahora?",
        "btn_check_updates": "Buscar actualizaciones",
        "update_none": "Tienes la versión más reciente ({cur}).",
        "update_check_failed": "No se pudieron buscar actualizaciones.\n"
                               "Comprueba la conexión.",
        "update_download_failed": "No se pudo descargar la actualización. \n"
                               "Comprueba la conexión y reintenta.",
        "update_ask_no_remind": "No recordar esta actualización",
        "update_yes": "Sí",
        "update_no": "No",
        "update_downloading": "Descargando la versión v{ver}…\n"
                             "Esta ventana se cerrará sola y el traductor "
                             "se reiniciará.",
        "update_restarting": "Actualización descargada. Reiniciando…",
        "update_failed": "No se pudo descargar la actualización.\n"
                         "¿Abrir la página de descarga?",
        "update_msg": "Nueva versión v{new} disponible (usted tiene v{cur}).\n\n"
                      "¿Abrir la página de descarga?\n"
                      "(El exe descargado puede reemplazar al anterior — la "
                      "configuración se conserva)",
        "instance_msg": "La aplicación ya está en ejecución.\n\n"
                        "Sí — activar la abierta\n"
                        "No — cerrarla e iniciar una nueva\n"
                        "Cancelar — no hacer nada",
        "dlg_clients_title": "Clientes — carpetas de logs",
        "col_name": "Nombre",
        "col_path": "Carpeta de logs",
        "btn_find_all": "Buscar todos",
        "btn_add": "Añadir…",
        "btn_remove": "Quitar",
        "btn_close": "Cerrar",
        "client_default": "Cliente {n}",
        "find_none": "No se encontraron carpetas de logs ni en Steam ni en el "
                     "cliente independiente.",
        "find_all_present": "Todas las carpetas encontradas ya están en la lista.",
        "add_dir_title": "Carpeta de logs — …\\<Jugador>\\logs",
        "kind_steam": "Steam",
        "kind_site": "web",
        "glossary_title": "Glosario de términos",
        "col_en": "Término en inglés",
        "col_ru": "Traducción",
        "btn_add_row": "➕ Añadir",
        "btn_delete_sel": "Eliminar seleccionados",
        "btn_open_file": "Abrir archivo…",
        "test_frame": "Traducir frase (como en el chat)",
        "glossary_status": "Entradas: {n} — cada cambio se guarda al instante "
                           "y aplica sin reiniciar",
        "glossary_save_err": "Error al guardar: {exc}",
        "err_output": "Error de salida: {exc}",
        "opt_sound": "Sonido de notificación:",
        "opt_channels": "Canales: traducción, sonido, notificaciones:",
        "opt_ch_translate": "traducir",
        "opt_ch_sound": "sonido",
        "opt_ch_notify": "notificaciones",
        "opt_channels_hint": "El clic derecho en la pestaña del canal hace lo mismo.",
        "btn_play": "Escuchar",
        "tab_menu_sound_pick": "Sonido de este canal",
        "sound_default": "Como el general",
        "sound_channel_default": "Sonido del canal",
        "sound_off": "Apagado",
        "alerts_log_btn": "Registro",
        "alerts_log_title": "Historial de alertas",
        "alerts_log_empty": "Aún no hay alertas.",
        "alerts_log_col_time": "Hora",
        "alerts_log_col_channel": "Canal",
        "alerts_log_col_text": "Texto",
        "snd_beep": "Estándar",
        "snd_chime": "Campana",
        "snd_bell": "Campanario",
        "snd_ding": "Ding",
        "snd_pop": "Pop",
        "snd_alarm": "Alarma",
    },
    "fr": {
        "app_title": "Wurm Chat — Traducteur",
        "clients_label": "Clients :",
        "opt_auto_copy": "Copier automatiquement la traduction de la réponse dans le presse-papiers",
        "clients_btn": "Clients…",
        "btn_settings": "Paramètres…",
        "alerts_btn": "Alertes…",
        "alerts_title": "Alertes de messages",
        "alerts_col_on": "Actif",
        "alerts_col_channel": "Canal",
        "alerts_col_pattern": "Sous-chaîne",
        "alerts_col_sound": "Son",
        "alerts_col_notify": "Notif.",
        "alerts_col_note": "Texte notif.",
        "alert_any": "Tous",
        "alerts_channel": "Canal :",
        "alerts_pattern": "Sous-chaîne :",
        "alerts_on": "Actif",
        "alert_sound": "Son",
        "alert_notify": "Notification",
        "alerts_add": "Nouveau",
        "alerts_save_rule": "Enregistrer",
        "alerts_remove": "Supprimer",
        "alerts_done": "Terminé",
        "alerts_hint": "Se déclenche quand la sous-chaîne apparaît dans le "
                       "canal (original et traduction vérifiés, sans "
                       "distinction de casse). L'onglet devient rouge ; une "
                       "règle se déclenche au plus une fois par 10 secondes. "
                       "Cliquez sur ✓/— pour basculer, sur le canal pour "
                       "ouvrir la liste des canaux ; cliquez sur « Son » pour "
                       "choisir un signal de la collection (ou le couper) ; "
                       "double-cliquez sur la sous-chaîne pour l'éditer.",
        "alert_toast_title": "Alerte",
        "lastalert_title": "Dernière alerte",
        "lastalert_jump": "Aller au message",
        "settings_title": "Paramètres de traduction",
        "opt_lang_channels": "Traduire les canaux de langue (Russian GL, English GL, …)",
        "opt_currency": "Traduire les unités monétaires (20s → 20 argent)",
        "settings_hint": "Les changements s'appliquent immédiatement, sans redémarrage.",
        "settings_close": "Fermer",
        "clients_none": "non définis",
        "clients_edit_hint": "Sélectionnez un client dans la liste — "
                               "le nom et le dossier sont modifiables "
                               "ci-dessous.",
        "btn_apply": "Appliquer",
        "btn_browse": "Parcourir…",
        "pick_title": "Choisir le dossier de logs",
        "pick_up": "Monter",
        "pick_choose": "Choisir ce dossier",
        "pick_status_ok": "Fichiers de journal : {n} ({files})",
        "pick_status_none": "Aucun fichier de journal — ce n'est pas le dossier de logs du jeu",
        "pick_status_bad": "Impossible de lire le dossier",
        "pick_sel_preview": "Dans « {name} »",
        "lang_label": "Langue :",
        "chk_translate": "Traduire en {lang}",
        "chk_sound": "Son",
        "chk_notify": "Notifications",
        "chk_original": "Original",
        "chk_on_top": "Toujours au premier plan",
        "tab_menu_translate": "Traduire ce canal",
        "tab_menu_sound": "Son de ce canal",
        "tab_menu_notify": "Notifications de ce canal",
        "tab_tt_sound_on": "Son pour nouveaux messages : activé (clic droit pour changer)",
        "tab_tt_sound_off": "Son pour nouveaux messages : désactivé (clic droit pour changer)",
        "tab_tt_notify_on": "Notifications pop-up : activées (clic droit pour changer)",
        "tab_tt_notify_off": "Notifications pop-up : désactivées (clic droit pour changer)",
        "reply_label": "Réponse :",
        "ctx_reply": "Répondre",
        "btn_start": "▶ Démarrer",
        "btn_stop": "■ Arrêter",
        "btn_clear": "Tout effacer",
        "btn_search": "🔍 Chercher",
        "btn_dig": "🕑 Passé",
        "dig_title": "Fouiller dans le passé",
        "dig_target": "Client : {ch}",
        "dig_mode_count": "derniers messages",
        "dig_mode_minutes": "minutes avant le lancement",
        "dig_label_count": "Combien de messages prendre :",
        "dig_label_minutes": "Combien de minutes avant le lancement :",
        "dig_pos": "Où les placer :",
        "dig_pos_start": "au début du chat",
        "dig_pos_end": "à la fin du chat",
        "dig_show": "Afficher",
        "dig_cancel": "Annuler",
        "dig_need_run": "Lancez d'abord la surveillance — le «passé» "
                        "n'existe qu'après.",
        "dig_empty": "Personne n'a écrit dans ce canal avant le lancement.",
        "dig_header_count": "⏳ Passé — {n} derniers messages",
        "dig_header_minutes": "⏳ Passé — {n} min avant le lancement",
        "dig_hint": "Traduire les anciens messages prend du temps — environ une "
                        "seconde par message.",
        "glossary_link": "Glossaire",
        "no_clients_hint": "Aucun client défini. Cliquez sur « Clients… » et "
                           "ajoutez le dossier de logs …\\players\\<Joueur>\\logs",
        "status_stopped": "Arrêté",
        "status_add_client": "Ajoutez au moins un client : bouton « Clients… »",
        "status_bad_path": "Dossier de logs indisponible : {path} — vérifiez "
                           "dans « Clients… »",
        "status_watching": "Surveillance de {n} client(s) : {names}. {mode}",
        "status_mode_tail": "nouveaux messages uniquement (anciens ignorés)",
        "status_running": "En marche. Messages capturés : {n}. {backend}",
        "status_copied": "Copié dans le presse-papiers : {text}",
        "status_translate_fail": "(échec de la traduction)",
        "backend_gtx": "traduction : Google ✓",
        "backend_mymemory": "traduction : service de secours",
        "backend_fallback": "traduction : Google en pause, service de secours",
        "backend_unknown": "traduction : aucune donnée",
        "watcher_restarted": "Observateur redémarré après un plantage (voir le "
                             "log)",
        "worker_restarted": "Traducteur redémarré après un plantage (voir le "
                            "log)",
        "msg_untranslated": "(échec de la traduction)",
        "search_title": "Recherche : {query} — trouvés : {n}",
        "search_empty": "Rien trouvé.",
        "search_hint": "Double-clic sur un résultat pour aller à son onglet",
        "tray_show": "Afficher",
        "tray_quit": "Quitter",
        "opt_auto_update": "Télécharger et installer les mises à jour "
                          "automatiquement",
        "update_dl_title": "Mise à jour du traducteur",
        "update_ask_title": "Nouvelle version disponible",
        "update_ask_msg": "Version {new} disponible (vous avez {cur}).\n"
                          "Télécharger et installer maintenant ?",
        "btn_check_updates": "Rechercher des mises à jour",
        "update_none": "Vous avez la dernière version ({cur}).",
        "update_check_failed": "Échec de la recherche de mises à jour.\n"
                               "Vérifiez la connexion.",
        "update_download_failed": "Impossible de télécharger la mise à jour. \n"
                               "Vérifiez la connexion et réessayez.",
        "update_ask_no_remind": "Ne plus me rappeler cette mise à jour",
        "update_yes": "Oui",
        "update_no": "Non",
        "update_downloading": "Téléchargement de la version v{ver}…\n"
                             "Cette fenêtre se fermera toute seule et le "
                             "traducteur redémarrera.",
        "update_restarting": "Mise à jour téléchargée. Redémarrage…",
        "update_failed": "Impossible de télécharger la mise à jour.\n"
                         "Ouvrir la page de téléchargement ?",
        "update_msg": "Nouvelle version v{new} disponible (vous avez v{cur}).\n\n"
                      "Ouvrir la page de téléchargement ?\n"
                      "(L'exe téléchargé peut remplacer l'ancien — les "
                      "réglages sont conservés)",
        "instance_msg": "L'application est déjà en cours d'exécution.\n\n"
                        "Oui — activer celle qui tourne\n"
                        "Non — la fermer et en lancer une nouvelle\n"
                        "Annuler — ne rien faire",
        "dlg_clients_title": "Clients — dossiers de logs",
        "col_name": "Nom",
        "col_path": "Dossier de logs",
        "btn_find_all": "Tout trouver",
        "btn_add": "Ajouter…",
        "btn_remove": "Supprimer",
        "btn_close": "Fermer",
        "client_default": "Client {n}",
        "find_none": "Aucun dossier de logs trouvé ni dans Steam ni dans le "
                     "client autonome.",
        "find_all_present": "Tous les dossiers trouvés sont déjà dans la liste.",
        "add_dir_title": "Dossier de logs — …\\<Joueur>\\logs",
        "kind_steam": "Steam",
        "kind_site": "site",
        "glossary_title": "Glossaire des termes",
        "col_en": "Terme anglais",
        "col_ru": "Traduction",
        "btn_add_row": "➕ Ajouter",
        "btn_delete_sel": "Supprimer la sélection",
        "btn_open_file": "Ouvrir le fichier…",
        "test_frame": "Traduire une phrase (comme dans le chat)",
        "glossary_status": "Entrées : {n} — chaque modification est enregistrée "
                           "immédiatement et s'applique sans redémarrage",
        "glossary_save_err": "Erreur d'enregistrement : {exc}",
        "err_output": "Erreur de sortie : {exc}",
        "opt_sound": "Son de notification :",
        "opt_channels": "Canaux : traduction, son, notifications :",
        "opt_ch_translate": "traduire",
        "opt_ch_sound": "son",
        "opt_ch_notify": "notifications",
        "opt_channels_hint": "Un clic droit sur l'onglet du canal fait la même chose.",
        "btn_play": "Écouter",
        "tab_menu_sound_pick": "Son de ce canal",
        "sound_default": "Comme le réglage général",
        "sound_channel_default": "Son du canal",
        "sound_off": "Désactivé",
        "alerts_log_btn": "Journal",
        "alerts_log_title": "Journal des alertes",
        "alerts_log_empty": "Aucune alerte pour le moment.",
        "alerts_log_col_time": "Heure",
        "alerts_log_col_channel": "Canal",
        "alerts_log_col_text": "Texte",
        "snd_beep": "Standard",
        "snd_chime": "Carillon",
        "snd_bell": "Cloche",
        "snd_ding": "Ding",
        "snd_pop": "Pop",
        "snd_alarm": "Alerte",
    },
    "zh": {
        "app_title": "Wurm 聊天翻译器",
        "opt_auto_copy": "自动将回复翻译复制到剪贴板",
        "clients_label": "客户端：",
        "clients_btn": "客户端…",
        "btn_settings": "设置…",
        "alerts_btn": "提醒…",
        "alerts_title": "消息提醒",
        "alerts_col_on": "启用",
        "alerts_col_channel": "频道",
        "alerts_col_pattern": "子串",
        "alerts_col_sound": "声音",
        "alerts_col_notify": "通知",
        "alerts_col_note": "通知文本",
        "alert_any": "任意",
        "alerts_channel": "频道：",
        "alerts_pattern": "子串：",
        "alerts_on": "启用",
        "alert_sound": "声音",
        "alert_notify": "通知",
        "alerts_add": "新建",
        "alerts_save_rule": "保存",
        "alerts_remove": "删除",
        "alerts_done": "完成",
        "alerts_hint": "当子串出现在频道中时触发（检查原文和译文，不区分"
                       "大小写）。标签页变红；同一规则每 10 秒最多触发一次。"
                       "单击 ✓/— 切换，单击频道打开频道选择列表，单击「声音」"
                       "从声音收藏中选择提示音（或关闭），双击子串进行编辑。",
        "alert_toast_title": "提醒",
        "lastalert_title": "最新提醒",
        "lastalert_jump": "转到消息",
        "settings_title": "翻译设置",
        "opt_lang_channels": "翻译语言频道（Russian GL、English GL 等）",
        "opt_currency": "翻译货币单位（20s → 20 银）",
        "settings_hint": "更改立即生效，无需重启。",
        "settings_close": "关闭",
        "clients_none": "未设置",
        "clients_edit_hint": "在列表中选择客户端——可在下方编辑名称和路径。",
        "btn_apply": "应用",
        "btn_browse": "浏览…",
        "pick_title": "选择日志文件夹",
        "pick_up": "向上",
        "pick_choose": "选择此文件夹",
        "pick_status_ok": "日志文件：{n} 个（{files}）",
        "pick_status_none": "没有日志文件——这不是游戏的 logs 文件夹",
        "pick_status_bad": "无法读取该文件夹",
        "pick_sel_preview": "在「{name}」中",
        "lang_label": "语言：",
        "chk_translate": "翻译为{lang}",
        "chk_sound": "声音",
        "chk_notify": "通知",
        "chk_original": "原文",
        "chk_on_top": "置顶",
        "tab_menu_translate": "翻译此频道",
        "tab_menu_sound": "此频道的声音",
        "tab_menu_notify": "此频道的通知",
        "tab_tt_sound_on": "新消息声音：开（右键切换）",
        "tab_tt_sound_off": "新消息声音：关（右键切换）",
        "tab_tt_notify_on": "弹出通知：开（右键切换）",
        "tab_tt_notify_off": "弹出通知：关（右键切换）",
        "reply_label": "回复：",
        "ctx_reply": "回复",
        "btn_start": "▶ 开始",
        "btn_stop": "■ 停止",
        "btn_clear": "全部清除",
        "btn_search": "🔍 搜索",
        "btn_dig": "🕑 过去",
        "dig_title": "挖掘过去",
        "dig_target": "客户端：{ch}",
        "dig_mode_count": "最近的消息",
        "dig_mode_minutes": "启动前多少分钟",
        "dig_label_count": "取多少条消息：",
        "dig_label_minutes": "启动前多少分钟：",
        "dig_pos": "输出位置：",
        "dig_pos_start": "当前聊天开头",
        "dig_pos_end": "当前聊天末尾",
        "dig_show": "显示",
        "dig_cancel": "取消",
        "dig_need_run": "请先启动监听——“过去”只在启动后存在。",
        "dig_empty": "启动前，此频道无人发言。",
        "dig_header_count": "⏳ 过去 — 最近 {n} 条消息",
        "dig_header_minutes": "⏳ 过去 — 启动前 {n} 分钟",
        "dig_hint": "翻译过去的消息需要时间——大约每条消息一秒。",
        "glossary_link": "词典",
        "no_clients_hint": "尚未设置客户端。点击「客户端…」添加日志文件夹 "
                           "…\\players\\<玩家>\\logs",
        "status_stopped": "已停止",
        "status_add_client": "请至少添加一个客户端：「客户端…」按钮",
        "status_bad_path": "日志文件夹不可用：{path} — 请在「客户端…」中检查",
        "status_watching": "正在监视 {n} 个客户端：{names}。{mode}",
        "status_mode_tail": "仅新消息（跳过旧消息）",
        "status_running": "运行中。已捕获消息：{n}。{backend}",
        "status_copied": "已复制到剪贴板：{text}",
        "status_translate_fail": "（翻译失败）",
        "backend_gtx": "翻译：Google ✓",
        "backend_mymemory": "翻译：备用服务",
        "backend_fallback": "翻译：Google 暂停中，使用备用服务",
        "backend_unknown": "翻译：无数据",
        "watcher_restarted": "监视器崩溃后已重启（见调试日志）",
        "worker_restarted": "翻译器崩溃后已重启（见调试日志）",
        "msg_untranslated": "（无法翻译）",
        "search_title": "搜索：{query} — 找到 {n} 条",
        "search_empty": "未找到结果。",
        "search_hint": "双击结果跳转到对应标签页",
        "tray_show": "显示",
        "tray_quit": "退出",
        "opt_auto_update": "自动下载并安装更新",
        "update_dl_title": "翻译器更新",
        "update_ask_title": "新版本可用",
        "update_ask_msg": "新版本 {new} 可用（当前 {cur}）。\n立即下载并安装？",
        "btn_check_updates": "检查更新",
        "update_none": "已是最新版本（{cur}）。",
        "update_check_failed": "检查更新失败。\n请检查网络连接。",
        "update_download_failed": "下载更新失败。\n"
                               "请检查网络连接后重试。",
        "update_ask_no_remind": "不再提醒此更新",
        "update_yes": "是",
        "update_no": "否",
        "update_downloading": "正在下载 v{ver} 版本…\n"
                             "此窗口将自动关闭，翻译器会重新启动。",
        "update_restarting": "更新已下载。正在重新启动…",
        "update_failed": "无法下载更新。\n打开下载页面？",
        "update_msg": "新版本 v{new} 可用（当前 v{cur}）。\n\n"
                      "打开下载页面？\n"
                      "（下载的 exe 可直接覆盖旧文件 — 设置会保留）",
        "instance_msg": "应用程序已在运行。\n\n"
                        "是 — 激活正在运行的实例\n"
                        "否 — 关闭它并启动新实例\n"
                        "取消 — 不做任何操作",
        "dlg_clients_title": "客户端 — 日志文件夹",
        "col_name": "名称",
        "col_path": "日志文件夹",
        "btn_find_all": "查找全部",
        "btn_add": "添加…",
        "btn_remove": "删除",
        "btn_close": "关闭",
        "client_default": "客户端 {n}",
        "find_none": "在 Steam 和独立客户端中均未找到日志文件夹。",
        "find_all_present": "所有找到的文件夹均已在列表中。",
        "add_dir_title": "日志文件夹 — …\\<玩家>\\logs",
        "kind_steam": "Steam",
        "kind_site": "独立客户端",
        "glossary_title": "游戏术语词典",
        "col_en": "英文术语",
        "col_ru": "翻译",
        "btn_add_row": "➕ 添加",
        "btn_delete_sel": "删除所选",
        "btn_open_file": "打开文件…",
        "test_frame": "翻译短语（与聊天相同）",
        "glossary_status": "词条：{n} — 每次修改立即保存，无需重启即可生效",
        "glossary_save_err": "保存错误：{exc}",
        "err_output": "输出错误：{exc}",
        "opt_sound": "通知声音：",
        "opt_channels": "各频道：翻译、声音、通知：",
        "opt_ch_translate": "翻译",
        "opt_ch_sound": "声音",
        "opt_ch_notify": "通知",
        "opt_channels_hint": "右键点击频道标签页可实现相同操作。",
        "btn_play": "试听",
        "tab_menu_sound_pick": "此频道的声音",
        "sound_default": "同全局",
        "sound_channel_default": "频道声音",
        "sound_off": "关闭",
        "alerts_log_btn": "日志",
        "alerts_log_title": "提醒记录",
        "alerts_log_empty": "暂无提醒。",
        "alerts_log_col_time": "时间",
        "alerts_log_col_channel": "频道",
        "alerts_log_col_text": "内容",
        "snd_beep": "默认",
        "snd_chime": "风铃",
        "snd_bell": "钟声",
        "snd_ding": "叮",
        "snd_pop": "噗",
        "snd_alarm": "警报",
    },
}


def tr(key, **kw):
    """Строка интерфейса на текущем языке; непереведённое — с русским."""
    s = I18N.get(UI_LANG, {}).get(key) or I18N["ru"].get(key) or key
    return s.format(**kw) if kw else s


def steam_language():
    """Язык интерфейса Steam из реестра: 'russian', 'english', … или None."""
    if platform.system() != "Windows":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Valve\Steam") as k:
            val, _t = winreg.QueryValueEx(k, "Language")
        return (val or "").strip().lower() or None
    except Exception:
        return None


def resolve_target_lang(cfg_value):
    """Код языка перевода из настройки ('auto' → язык Steam → русский)."""
    if cfg_value and cfg_value != "auto":
        return cfg_value if cfg_value in SUPPORTED_LANGS else "ru"
    return STEAM_LANG_MAP.get(steam_language() or "", "ru")


def resolve_ui_lang(cfg_value):
    """Код языка интерфейса (для tr): из настройки, 'auto' → язык Steam."""
    return resolve_target_lang(cfg_value)


# --------------------------------------------------------------------------- #
# Графический интерфейс
# --------------------------------------------------------------------------- #

def make_tray_icon():
    """Синяя плашка с белой «W» из линий — 64x64."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=12, fill=(0, 102, 255, 255))
    d.line((18, 18, 26, 46, 32, 30, 38, 46, 46, 18), fill=(255, 255, 255, 255), width=5, joint="curve")
    return img


def run_gui():
    import tkinter as tk
    from tkinter import scrolledtext, ttk

    cfg = load_config()
    target_lang = resolve_target_lang(cfg.get("target_lang"))
    # Язык интерфейса = язык перевода; без явного выбора — по языку Steam
    global UI_LANG
    UI_LANG = resolve_ui_lang(cfg.get("target_lang"))

    root = tk.Tk()
    root.title(f"{tr('app_title')} v{APP_VERSION}")
    root.geometry("760x560")
    root.minsize(560, 380)

    # Ошибки обработчиков Tk (клики и т.п.) по умолчанию уходят в stderr —
    # в оконном приложении это «никуда». Пишем их в журнал отладки.
    def _tk_error(exc_type, exc, tb):
        log_debug(f"ОШИБКА ОБРАБОТЧИКА Tk: {exc_type.__name__}: {exc}\n"
                  + "".join(traceback.format_tb(tb)))
    root.report_callback_exception = _tk_error

    stop_event = threading.Event()
    state = {"running": False, "watchers": [], "worker": None,
             "raw": queue.Queue(), "done": queue.Queue()}

    # ---------------- верхняя панель ----------------
    top = ttk.Frame(root)
    top.pack(fill="x", padx=8, pady=(8, 2))
    ttk.Label(top, text=tr("clients_label")).pack(side="left")
    clients_var = tk.StringVar()

    def clients_summary():
        cls = cfg.get("clients") or []
        if not cls:
            return tr("clients_none")
        if len(cls) == 1:
            return cls[0].get("name") or cls[0].get("path", "")
        return ", ".join(c.get("name") or c.get("path", "") for c in cls)

    ttk.Label(top, textvariable=clients_var, foreground="#555555").pack(
        side="left", padx=6)
    ttk.Button(top, text=tr("clients_btn"),
               command=lambda: open_clients_dialog()).pack(side="left")
    ttk.Button(top, text=tr("btn_settings"),
               command=lambda: open_settings_dialog()).pack(side="left", padx=(6, 0))
    ttk.Button(top, text=tr("alerts_btn"),
               command=lambda: open_alerts_dialog()).pack(side="left", padx=(6, 0))
    ttk.Button(top, text=tr("alerts_log_btn"),
               command=lambda: open_alert_log_dialog()).pack(side="left", padx=(6, 0))
    ttk.Label(top, text=tr("lang_label")).pack(side="left", padx=(14, 2))
    # Названия языков — на родных языках: случайно выбравший китайский
    # всегда найдёт «Русский» / «English» в этом списке обратно
    lang_var = tk.StringVar()
    lang_combo = ttk.Combobox(top, textvariable=lang_var, state="readonly",
                              width=11, values=[name for _c, name in LANGUAGES])
    lang_combo.pack(side="left")
    cur_setting = cfg.get("target_lang") or "auto"
    if cur_setting not in [c for c, _n in LANGUAGES]:
        cur_setting = "auto"
    lang_var.set(dict(LANGUAGES)[cur_setting])

    def on_lang_change(_e=None):
        code = {name: c for c, name in LANGUAGES}.get(lang_var.get(), "auto")
        if code == (cfg.get("target_lang") or "auto"):
            return
        cfg["target_lang"] = code
        # источник панели ответа следует за основным языком: сбрасываем
        # сохранённый выбор — после перезапуска встанет новый язык
        cfg["reply_src"] = ""
        save_config(cfg)
        restart_gui()

    lang_combo.bind("<<ComboboxSelected>>", on_lang_change)

    # ---------------- панель флагов ----------------
    flags = ttk.Frame(root)
    flags.pack(fill="x", padx=8, pady=2)

    def flag_toggled(key, var):
        """Галочка изменена — запоминаем сразу: состояние не должно
        зависеть от корректного выхода (приложение могут просто убить)."""
        cfg[key] = bool(var.get())
        save_config(cfg)

    tr_var = tk.BooleanVar(value=cfg["translate"])
    ttk.Checkbutton(flags, text=tr("chk_translate",
                                    lang=LANG_OBJ[UI_LANG][target_lang]),
                    variable=tr_var,
                    command=lambda: flag_toggled("translate", tr_var)) \
        .pack(side="left")
    orig_var = tk.BooleanVar(value=cfg["show_original"])
    ttk.Checkbutton(flags, text=tr("chk_original"),
                    variable=orig_var,
                    command=lambda: flag_toggled("show_original", orig_var)) \
        .pack(side="left", padx=8)
    top_var = tk.BooleanVar(value=cfg["on_top"])
    top_chk = ttk.Checkbutton(flags, text=tr("chk_on_top"), variable=top_var)
    top_chk.pack(side="left")
    root.attributes("-topmost", cfg["on_top"])

    def toggle_on_top():
        root.attributes("-topmost", top_var.get())
        flag_toggled("on_top", top_var)
    top_chk.config(command=toggle_on_top)

    # ---------------- вкладки-окна каналов ----------------
    # Два уровня вкладок: верхний — клиенты (по одному на каждую копию игры,
    # для мультиоконной игры), внутри каждого — привычные вкладки каналов.
    # Классические tk.Label: у ttk.Notebook нельзя перекрасить вкладку
    # в ярко-синий цвет «непрочитано».
    TAB_FG = "#404040"          # обычный цвет заголовка вкладки
    TAB_FG_UNREAD = "#0066ff"   # ярко-синий — новые сообщения
    TAB_FG_ALERT = "#cc0000"    # красный — сработал сигнал (важнее синего)
    TAB_BG_ACTIVE = "#cfe3f5"   # фон активной вкладки

    clients_holder = tk.Frame(root)
    clients_holder.pack(fill="both", expand=True, padx=8, pady=4)

    clients_ui = []            # по индексу клиента: его вкладки и окна каналов
    current_client = {"idx": 0}
    history = {}               # (индекс клиента, канал) -> список сообщений
    past = {}                  # (индекс клиента, канал) -> {"entries", "pos",
                               # "header", "shown"} — блок «прошлого» из
                               # кнопки «Порыться в прошлом» (строки лога,
                               # записанные до запуска переводчика)
    txt_owner = {}             # виджет чата -> (индекс клиента, канал) — для ПКМ/подсветки
    alerted = {"tabs": set(), "clients": set()}   # подсвеченные красным сигналом
    alert_state = {"sound": False}   # сигнал уже пикнул — обычный звук не нужен
    import collections
    alert_log = collections.deque(maxlen=200)   # журнал срабатываний сигналов
    entry_starts = {}          # виджет чата -> номера строк, с которых начинаются
                               # записи history (в том же порядке) — для ПКМ

    def chat_context_menu(event, client_idx, channel):
        """ПКМ по сообщению: «Ответить» — переключить панель ответа на язык,
        с которого это сообщение было переведено, и дать фокус ввода."""
        txt = event.widget
        try:
            line = int(str(txt.index(f"@{event.x},{event.y}")).split(".")[0])
        except Exception:
            return
        starts = entry_starts.get(txt) or []
        idx = None
        for i, s in enumerate(starts):
            if s <= line:
                idx = i
            else:
                break
        if idx is None:
            log_debug(f"пкм {channel}: строка {line} вне записей "
                      f"(starts={starts[:5]}…)")
            return
        entries = history.get((client_idx, channel)) or []
        if idx >= len(entries):
            log_debug(f"пкм {channel}: idx={idx} >= записей {len(entries)}")
            return
        _t, _name, original, translated, src = entries[idx]
        # меню только для реально переведённых с иностранного языка сообщений
        if not (translated and translated != original
                and src in SUPPORTED_LANGS):
            log_debug(f"пкм {channel}: меню скрыто — src={src!r}, "
                      f"translated={translated!r:.80}, original={original!r:.80}")
            return
        log_debug(f"пкм {channel}: меню показано, src={src!r}")

        def reply_to():
            log_debug(f"пкм {channel}: «ответить» нажато, dst={src}")
            reply_dst_var.set(src)
            reply_save_langs()          # сохранит выбор и пересчитает текст
            reply_entry.focus_set()     # курсор — сразу в поле ответа

        menu = tk.Menu(root, tearoff=0)
        menu.add_command(label=tr("ctx_reply"), command=reply_to)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def switch_client(idx):
        current_client["idx"] = idx
        clients_ui[idx]["frame"].lift()
        # открыли вкладку клиента — признак «непрочитано» и сигнал сняты
        alerted["clients"].discard(idx)
        if clients_ui[idx]["btn"] is not None:
            clients_ui[idx]["btn"].config(fg=TAB_FG)
        for i, ui in enumerate(clients_ui):
            b = ui["btn"]
            if b is None:
                continue
            if i == idx:
                b.config(bg=TAB_BG_ACTIVE, relief="sunken")
            else:
                b.config(bg=root.cget("bg"), relief="flat")

    def switch_tab(client_idx, name):
        ui = clients_ui[client_idx]
        ui["current"]["name"] = name
        _, frame = ui["tabs"][name]
        frame.lift()
        # открыли вкладку — «непрочитано» и красная подсветка сигнала сняты
        alerted["tabs"].discard((client_idx, name))
        for n, b in ui["tab_buttons"].items():
            if n == name:
                b.config(bg=TAB_BG_ACTIVE, relief="sunken")
                b.config(fg=TAB_FG)
            else:
                b.config(bg=root.cget("bg"), relief="flat")

    def active_channel(client_idx):
        return clients_ui[client_idx]["current"]["name"]

    # ---------------- копирование/вставка Ctrl-C / Ctrl-V ----------------
    # У read-only Text (state="disabled") фокуса нет — собственные биндинги
    # Ctrl-C не срабатывают. Поэтому запоминаем, в каком текстовом поле
    # последний раз выделяли текст мышью, и глобально ловим Ctrl-C: если в
    # том поле есть выделение — копируем его; если нет — копируется выделение
    # поля ввода под фокусом. Реальные Ctrl+C/Ctrl+V на Windows Tk приходят
    # с keysym «??» и char «\x03»/«\x16», поэтому нативные привязки
    # <Control-c>/<Control-v> не срабатывают — ловим по символу.
    last_sel_text = {"w": None}

    def _track_selection(event):
        w = event.widget
        if isinstance(w, tk.Text):
            last_sel_text["w"] = w if w.tag_ranges("sel") else None
        elif w is not root and hasattr(w, "selection_present"):
            # клик по полю ввода — его выделение важнее чатового
            last_sel_text["w"] = None
        return None

    def _focused_entry(event):
        """Виджет под фокусом, если это редактируемое поле ввода."""
        fw = getattr(event, "widget", None)
        if fw is None:
            return None
        try:
            if str(fw.cget("state")) != "normal":
                return None          # disabled/readonly — не трогаем
        except tk.TclError:
            return None
        if isinstance(fw, (tk.Entry, ttk.Entry)):
            return fw
        return None

    def _global_ctrl_c(event):
        fw = getattr(event, "widget", None)
        # поле ввода с собственным выделением — копируем из него
        if fw is not None and hasattr(fw, "selection_present"):
            try:
                if fw.selection_present():
                    sel = fw.selection_get()
                    fw.clipboard_clear()
                    fw.clipboard_append(sel)
                    return "break"
            except tk.TclError:
                pass
        # иначе — выделение в чате, где выделяли в последний раз мышью
        w = last_sel_text["w"]
        try:
            if (w is None or not w.winfo_exists()
                    or not w.tag_ranges("sel")):
                return None          # выделения в чатах нет — не мешаем
        except tk.TclError:          # виджет уже уничтожен
            last_sel_text["w"] = None
            return None
        sel = w.get("sel.first", "sel.last")
        w.clipboard_clear()
        w.clipboard_append(sel)
        return "break"

    def _global_ctrl_v(event):
        ent = _focused_entry(event)
        if ent is None:
            return None              # фокус не в редактируемом поле
        try:
            clip = ent.clipboard_get()
        except tk.TclError:
            return None              # буфер пуст или недоступен
        if not clip:
            return None
        try:
            if ent.selection_present():   # вставка поверх выделения
                first = ent.index("sel.first")
                ent.delete("sel.first", "sel.last")
                ent.icursor(first)        # курсор — на место выделения
        except tk.TclError:
            pass
        ent.insert("insert", clip)
        return "break"

    def _global_ctrl_x(event):
        ent = _focused_entry(event)
        if ent is None:
            return None
        try:
            if not ent.selection_present():
                return None
            sel = ent.selection_get()
            ent.clipboard_clear()
            ent.clipboard_append(sel)
            ent.delete("sel.first", "sel.last")
            return "break"
        except tk.TclError:
            return None

    def _on_any_key(event):
        # Реальные Ctrl+C/V/X на Windows Tk приходят как KeyPress с keysym
        # «??» и char «\x03»/«\x16»/«\x18» — шаблоны <Control-…> их не ловят.
        if event.char == "\x03":
            return _global_ctrl_c(event)
        if event.char == "\x16":
            return _global_ctrl_v(event)
        if event.char == "\x18":
            return _global_ctrl_x(event)
        return None

    # Ctrl-C доходит до Tcl по-разному: как char «\x03», как <Control-c>
    # (синтетические события) или как <<Copy>> (Ctrl+Insert) — слушаем всё.
    root.bind_all("<ButtonRelease-1>", _track_selection, add="+")
    root.bind_all("<Key>", _on_any_key, add="+")
    root.bind_all("<Control-c>", _global_ctrl_c)
    root.bind_all("<Control-C>", _global_ctrl_c)
    root.bind_all("<<Copy>>", _global_ctrl_c)

    def rebuild_clients_area():
        """Пересоздать область вкладок по текущему списку клиентов.

        История хранится отдельно от виджетов — окна каналов заполняются
        заново из history, поэтому пересоздание безболезненно.
        """
        for w in clients_holder.winfo_children():
            w.destroy()
        clients_ui.clear()
        clients = cfg.get("clients") or []
        n = len(clients)
        if n == 0:
            tk.Label(clients_holder, foreground="#666666",
                     text=tr("no_clients_hint")).pack(
                     anchor="w", padx=8, pady=8)
            return
        if n > 1:
            bar = tk.Frame(clients_holder)
            bar.pack(fill="x")
            cont = tk.Frame(clients_holder)
            cont.pack(fill="both", expand=True)
        else:
            bar = None
            cont = clients_holder
        for i, cl in enumerate(clients):
            cframe = tk.Frame(cont)
            if n > 1:
                # фреймы клиентов накладываются друг на друга (place), видимый
                # выбирается через lift() в switch_client()
                cframe.place(relx=0, rely=0, relwidth=1, relheight=1)
                cbtn = tk.Label(bar, text=cl.get("name") or tr("client_default", n=i + 1),
                                fg=TAB_FG, padx=10, pady=3, cursor="hand2",
                                font=("Segoe UI", 9, "bold"))
                cbtn.pack(side="left")
                cbtn.bind("<Button-1>", lambda _e, idx=i: switch_client(idx))
            else:
                # единственный клиент — просто растягиваем на всю область,
                # иначе фрейм останется невидимым (нет геометрии — нет окна)
                cframe.pack(fill="both", expand=True)
                cbtn = None
            tabbar = tk.Frame(cframe)
            tabbar.pack(fill="x")
            tab_state = {"bar": tabbar, "groups": [], "rows": [],
                         "width": 0, "job": None}
            tabbar.bind("<Configure>",
                        lambda _e, s=tab_state: on_tabbar_configure(s))
            content = ttk.Frame(cframe)
            content.pack(fill="both", expand=True, pady=(2, 0))
            tabs = {}
            tab_buttons = {}
            tab_marks = {}
            for name, _, _ in CHANNELS:
                frame = ttk.Frame(content)
                txt = scrolledtext.ScrolledText(frame, wrap="word", state="disabled",
                                                font=("Segoe UI", 10))
                txt.pack(fill="both", expand=True)
                txt.tag_config("time", foreground="#005a9e")
                txt.tag_config("name", font=("Segoe UI", 10, "bold"))
                txt.tag_config("orig", foreground="#777777")
                txt.tag_config("err", foreground="#cc0000")
                # строки «прошлого» (кнопка «Порыться в прошлом»): курсив,
                # приглушённый цвет, лёгкий сепиевый фон — сразу видно,
                # что это не свежие сообщения
                txt.tag_config("past", foreground="#6f7480",
                               background="#f5efdc",
                               font=("Segoe UI", 10, "italic"))
                txt.tag_config("past_head", foreground="#8a6d1d",
                               background="#f5efdc",
                               font=("Segoe UI", 9, "bold italic"))
                # упоминание имени персонажа — мягкий жёлтый фон сообщения
                txt.tag_config("mention", background="#ffedad")
                txt.tag_lower("mention")
                bind_copy(txt)
                txt.bind("<Button-3>", lambda e, idx=i, ch=name:
                         chat_context_menu(e, idx, ch))
                # фреймы каналов накладываются друг на друга (place, НЕ pack —
                # pack разложил бы их полосами), видимый — через lift()
                frame.place(relx=0, rely=0, relwidth=1, relheight=1)
                tabs[name] = (txt, frame)
                txt_owner[txt] = (i, name)
                btn = tk.Label(tabbar, text=name, fg=TAB_FG, pady=3,
                               cursor="hand2", font=("Segoe UI", 8))
                btn.bind("<Button-1>",
                         lambda _e, n=name, idx=i: switch_tab(idx, n))
                btn.bind("<Button-3>", lambda e, idx=i, ch=name:
                         channel_tab_menu(e, idx, ch))
                tab_buttons[name] = btn
                # значки разрешений канала — ПАССИВНЫЕ индикаторы: кликать
                # их нельзя (случайные нажатия мимо вкладки выключали звук
                # незаметно для пользователя). Переключение — ПКМ по
                # вкладке (channel_tab_menu) или в «Настройки». Звук — нота
                # ♪, уведомления — конверт ✉: одна гарнитура, один размер
                # и одинаковые цвета (тёмный = вкл, серый = выкл).
                # Отступ — только между каналами.
                ic_s = tk.Label(tabbar, text="♪",
                                font=("Segoe UI Symbol", 9), fg="#b3b3b3")
                widget_tooltip(
                    ic_s,
                    lambda ch=name: tr("tab_tt_sound_on")
                    if channel_flag(cfg.get("sound_channels"), ch)
                    else tr("tab_tt_sound_off"))
                ic_n = tk.Label(tabbar, text="✉",
                                font=("Segoe UI Symbol", 9), fg="#b3b3b3")
                widget_tooltip(
                    ic_n,
                    lambda ch=name: tr("tab_tt_notify_on")
                    if channel_flag(cfg.get("notify_channels"), ch)
                    else tr("tab_tt_notify_off"))
                tab_marks[name] = (ic_s, ic_n)
                # в строку панели вкладок НЕ укладываем — раскладку по
                # строкам (с переносом при нехватке ширины) делает
                # layout_tab_rows()
                tab_state["groups"].append((btn, ic_s, ic_n))
            clients_ui.append({"btn": cbtn, "frame": cframe, "tabs": tabs,
                               "tab_buttons": tab_buttons, "tab_marks": tab_marks,
                               "tab_state": tab_state,
                               "current": {"name": CHANNELS[0][0]}})
        current_client["idx"] = 0
        # у каждого клиента поднять его активную вкладку канала — иначе
        # поверх лежит последний созданный фрейм (PM) и окно выглядит пустым
        for i in range(len(clients_ui)):
            switch_tab(i, CHANNELS[0][0])
        if n > 1:
            switch_client(0)
        # восстановили окна — выводим накопленную историю заново
        for i in range(len(clients_ui)):
            for ch in clients_ui[i]["tabs"]:
                rebuild_channel(i, ch)
        refresh_tab_marks()
        # первоначальная раскладка вкладок по строкам (дальше — по Configure)
        for ui in clients_ui:
            st = ui.get("tab_state")
            if st:
                st["job"] = st["bar"].after(
                    100, lambda s=st: layout_tab_rows(s))

    def refresh_tab_marks():
        """Значки разрешений на вкладках каналов: нота ♪ — звук, конверт ✉ —
        уведомления; одинаковые цвета: тёмный = вкл, серый = выкл. По
        умолчанию (списки пустые) — всё выключено."""
        for ui in clients_ui:
            for name, icons in (ui.get("tab_marks") or {}).items():
                ic_s, ic_n = icons
                s_on = channel_flag(cfg.get("sound_channels"), name)
                n_on = channel_flag(cfg.get("notify_channels"), name)
                try:
                    ic_s.config(text="♪", fg="#222222" if s_on else "#b3b3b3")
                    ic_n.config(text="✉", fg="#222222" if n_on else "#b3b3b3")
                except tk.TclError:
                    pass

    def toggle_channel_flag(key, channel):
        """Вкл/выкл звука или уведомлений канала (контекстное меню
        вкладки, зеркало в «Настройках»)."""
        set_channel_flag(cfg, key, channel,
                         not channel_flag(cfg.get(key), channel))
        save_config(cfg)
        refresh_tab_marks()

    def channel_sound_path(channel):
        """Звук канала: выбор канала важнее общего; общий — встроенный звук
        из настроек. Ничего не найдено — None (сигнал по умолчанию)."""
        sid = (cfg.get("channel_sounds") or {}).get(channel) \
            or cfg.get("sound") or "beep"
        return resolve_sound_path(sid, cfg.get("sound_file") or "")

    def channel_tab_menu(event, client_idx, channel):
        """ПКМ по вкладке канала: переводить ли канал, звук и уведомления
        именно из этого канала."""
        menu = tk.Menu(root, tearoff=0)
        # галочка = «переводить», в конфиге храним список каналов БЕЗ перевода
        var_t = tk.BooleanVar(
            value=not channel_flag(cfg.get("no_translate_channels"), channel))
        var_s = tk.BooleanVar(
            value=channel_flag(cfg.get("sound_channels"), channel))
        var_n = tk.BooleanVar(
            value=channel_flag(cfg.get("notify_channels"), channel))

        def apply(key, var):
            set_channel_flag(cfg, key, channel, bool(var.get()))
            save_config(cfg)
            refresh_tab_marks()

        def apply_translate():
            set_channel_flag(cfg, "no_translate_channels", channel,
                             not bool(var_t.get()))
            save_config(cfg)
            refresh_tab_marks()

        menu.add_checkbutton(label=tr("tab_menu_translate"), variable=var_t,
                             command=apply_translate)
        menu.add_checkbutton(label=tr("tab_menu_sound"), variable=var_s,
                             command=lambda: apply("sound_channels", var_s))
        menu.add_checkbutton(label=tr("tab_menu_notify"), variable=var_n,
                             command=lambda: apply("notify_channels", var_n))

        # выбор звука именно этого канала (важнее общего из настроек)
        var_pick = tk.StringVar(
            value=(cfg.get("channel_sounds") or {}).get(channel) or "")

        def apply_sound():
            sounds = dict(cfg.get("channel_sounds") or {})
            pick = var_pick.get()
            if pick:
                sounds[channel] = pick
            else:
                sounds.pop(channel, None)      # «как общий» — запись не нужна
            cfg["channel_sounds"] = sounds
            save_config(cfg)

        sub = tk.Menu(menu, tearoff=0)
        sub.add_radiobutton(label=tr("sound_default"), variable=var_pick,
                            value="", command=apply_sound)
        for sid in BUILTIN_SOUNDS:
            sub.add_radiobutton(label=tr(f"snd_{sid}"), variable=var_pick,
                                value=sid, command=apply_sound)
        menu.add_cascade(label=tr("tab_menu_sound_pick"), menu=sub)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ---------------- панель ответа (перевод в обе стороны) ----------------
    reply = ttk.Frame(root)
    reply.pack(fill="x", padx=8, pady=(2, 0))
    ttk.Label(reply, text=tr("reply_label")).pack(side="left")
    # компактные списки: коды языков, чтобы не раздувать панель.
    # Источник по умолчанию = основной язык перевода; свой выбор сохраняется.
    reply_src_var = tk.StringVar(
        value=cfg.get("reply_src") or target_lang or "ru")
    reply_dst_var = tk.StringVar(value=cfg.get("reply_dst") or "en")
    reply_src = ttk.Combobox(reply, textvariable=reply_src_var, values=SUPPORTED_LANGS,
                             state="readonly", width=4)
    reply_src.pack(side="left", padx=(4, 0))
    ttk.Label(reply, text="→").pack(side="left", padx=2)
    reply_dst = ttk.Combobox(reply, textvariable=reply_dst_var, values=SUPPORTED_LANGS,
                             state="readonly", width=4)
    reply_dst.pack(side="left")
    reply_var = tk.StringVar()
    reply_entry = ttk.Entry(reply, textvariable=reply_var, width=32)
    reply_entry.pack(side="left", padx=(8, 8))
    reply_out_var = tk.StringVar(value="")
    reply_out = ttk.Entry(reply, textvariable=reply_out_var, state="readonly",
                          foreground="#005a9e")
    reply_out.pack(side="left", fill="x", expand=True)

    reply_state = {"after": None, "seq": 0}

    def reply_langs():
        src = reply_src_var.get() if reply_src_var.get() in SUPPORTED_LANGS \
            else (target_lang or "ru")
        dst = reply_dst_var.get() if reply_dst_var.get() in SUPPORTED_LANGS else "en"
        return src, dst

    def reply_save_langs(_evt=None):
        src, dst = reply_langs()
        if src != cfg.get("reply_src") or dst != cfg.get("reply_dst"):
            cfg["reply_src"], cfg["reply_dst"] = src, dst
            save_config(cfg)
        # язык сменили — пересчитываем уже введённый текст
        reply_on_change()

    reply_src.bind("<<ComboboxSelected>>", reply_save_langs)
    reply_dst.bind("<<ComboboxSelected>>", reply_save_langs)

    def reply_translate_async(text):
        """Перевод в фоне, результат — в GUI-поток."""
        seq = reply_state["seq"]
        src, dst = reply_langs()
        try:
            gl_terms = []
            if src == "ru" and dst == "en":
                # термины прячем до перевода: иначе Google коверкает их
                # («вемп» -> «hemp»), а пост-замене не за что зацепиться
                text, gl_terms = glossary_protect_ru(text)
            if src == dst:
                result = text        # языки совпали — переводить нечего
            else:
                result, _lang = translate_auto(text, dst=dst, src=src)
            if result:
                if gl_terms:
                    result = glossary_restore(result, gl_terms)
                # игровые термины — принятыми словами, а не дословно;
                # словарь знает только пару ru↔en
                if dst == "en" and src == "ru":
                    result = glossary_fix(result, dst="en")
                elif dst == "ru":
                    result = glossary_fix(result, dst="ru")
        except Exception:
            result = None
        root.after(0, lambda: reply_show_result(text, result, seq))

    def reply_show_result(text, result, seq):
        if seq != reply_state["seq"]:
            return          # устаревший запрос — текст уже изменился
        if result is None:
            reply_out_var.set(tr("status_translate_fail"))
            return
        reply_out_var.set(result)
        if not cfg.get("auto_copy_reply", True):
            return          # опция выключена — перевод только на экране
        try:
            root.clipboard_clear()
            root.clipboard_append(result)
            status_var.set(tr("status_copied", text=result[:60]))
        except Exception:
            pass

    def reply_on_change(*_args):
        text = reply_var.get().strip()
        reply_state["seq"] += 1
        if reply_state["after"] is not None:
            root.after_cancel(reply_state["after"])
            reply_state["after"] = None
        if not text:
            reply_out_var.set("")
            return
        # переводим через 0,7 с после последней введённой буквы
        reply_state["after"] = root.after(
            700, lambda: threading.Thread(
                target=reply_translate_async, args=(text,), daemon=True).start())

    reply_var.trace_add("write", reply_on_change)

    # ---------------- нижняя панель ----------------
    bottom = ttk.Frame(root)
    bottom.pack(fill="x", padx=8, pady=(2, 8))
    # два ряда: статус — отдельно, чтобы длинный текст статуса не вытеснял
    # кнопки поиска/раскопок за край окна на узкой ширине
    status_row = ttk.Frame(bottom)
    status_row.pack(fill="x")
    controls_row = ttk.Frame(bottom)
    controls_row.pack(fill="x", pady=(2, 0))
    status_var = tk.StringVar(value=tr("status_stopped"))
    ttk.Label(status_row, textvariable=status_var).pack(side="left")
    # номер версии — тихий уголок справа; рядом неприметная ссылка на словарь.
    # Словарь — механизм пост-обработки только для русского направления,
    # поэтому ссылку показываем лишь когда перевод идёт на русский.
    ttk.Label(status_row, text=f"v{APP_VERSION}",
              foreground="#999999").pack(side="right", padx=(4, 0))
    if target_lang == "ru":
        glossary_link = tk.Label(status_row, text=tr("glossary_link"), fg="#7a9cc4",
                                 cursor="hand2", font=("Segoe UI", 8))
        glossary_link.pack(side="right", padx=(4, 4))
        glossary_link.bind("<Button-1>",
                           lambda _e: open_glossary_editor(root, target_lang))
    run_btn = ttk.Button(controls_row, text=tr("btn_start"))
    run_btn.pack(side="right", padx=(4, 0))
    def clear_all():
        for key in list(history):
            history[key].clear()
        for ui in clients_ui:
            for _ch, (w, _f) in ui["tabs"].items():
                w.config(state="normal")
                w.delete("1.0", "end")
                w.config(state="disabled")
                entry_starts[w] = []

    clear_btn = ttk.Button(controls_row, text=tr("btn_clear"), command=clear_all)
    clear_btn.pack(side="right", padx=4)

    # ---------------- поиск по истории ----------------
    def do_search(event=None):
        query = search_var.get().strip().lower()
        if not query:
            return
        results = []
        for (ci, ch_name), entries in history.items():
            for (t, name, original, translated, _src) in entries:
                hay = " ".join(x for x in (name, original, translated) if x).lower()
                if query in hay:
                    results.append((ci, ch_name, t, name, original, translated))
        show_search_results(query, results)

    def show_search_results(query, results):
        win = tk.Toplevel(root)
        win.title(tr("search_title", query=query, n=len(results)))
        win.geometry("640x400")
        txt = scrolledtext.ScrolledText(win, wrap="word", state="disabled",
                                        font=("Segoe UI", 10))
        txt.pack(fill="both", expand=True)
        txt.tag_config("time", foreground="#005a9e")
        txt.tag_config("name", font=("Segoe UI", 10, "bold"))
        txt.tag_config("ch", foreground="#0066ff")
        txt.tag_config("orig", foreground="#777777")
        bind_copy(txt)

        def result_label(ci, ch_name):
            cls = cfg.get("clients") or []
            if len(cls) > 1 and ci < len(cls):
                return (f"{cls[ci].get('name') or tr('client_default', n=ci + 1)}"
                        f" · {ch_name}")
            return ch_name

        txt.config(state="normal")
        if not results:
            txt.insert("end", tr("search_empty") + "\n")
        lines = []   # индекс строки -> (клиент, канал) для двойного клика
        for (ci, ch_name, t, name, original, translated) in results:
            txt.insert("end", f"[{result_label(ci, ch_name)}] ", "ch")
            if t:
                txt.insert("end", f"[{t}] ", "time")
            if name:
                txt.insert("end", f"{name}: ", "name")
            txt.insert("end", (translated or original) + "\n")
            if translated and translated != original and orig_var.get():
                txt.insert("end", original + "\n", "orig")
            txt.insert("end", "\n")
            lines.append((ci, ch_name))
        txt.config(state="disabled")

        def on_double(_e):
            try:
                idx = txt.index(f"@{_e.x},{_e.y}")
                line_no = int(idx.split(".")[0])
            except Exception:
                return
            # строки результата идут парами/тройками, ищем ближайший сверху канал
            target = None
            for ln in range(line_no, 0, -1):
                content = txt.get(f"{ln}.0", f"{ln}.end")
                if content.startswith("["):
                    m = re.match(r"\[([^\]]+)\]", content)
                    if m:
                        for ci, ch_name in lines:
                            if m.group(1) == result_label(ci, ch_name):
                                target = (ci, ch_name)
                                break
                    if target:
                        break
            if target:
                ci, ch_name = target
                if ci < len(clients_ui):
                    if len(clients_ui) > 1:
                        switch_client(ci)
                    switch_tab(ci, ch_name)

        txt.bind("<Double-Button-1>", on_double)
        ttk.Label(win, text=tr("search_hint"),
                  foreground="#666666").pack(anchor="w", padx=6, pady=2)

    search_var = tk.StringVar()
    search_entry = ttk.Entry(controls_row, textvariable=search_var, width=24)
    search_entry.pack(side="left", padx=(0, 2))
    search_entry.bind("<Return>", do_search)
    ttk.Button(controls_row, text=tr("btn_search"),
               command=do_search).pack(side="left")
    # «Порыться в прошлом»: строки лога до запуска переводчика блоком
    # со своей раскраской; lambda — функция объявлена ниже по файлу
    ttk.Button(controls_row, text=tr("btn_dig"),
               command=lambda: open_dig_dialog()
               ).pack(side="left", padx=(10, 0))

    # ---------------- логика ----------------
    def collect_cfg():
        cfg.update({
            "clients": list(cfg.get("clients") or []),
            "translate": tr_var.get(),
            "show_original": orig_var.get(),
            "on_top": top_var.get(),
            "translate_currency": bool(cfg.get("translate_currency", True)),
            "auto_copy_reply": bool(cfg.get("auto_copy_reply", True)),
        })
        save_config(cfg)

    HISTORY_MAX = 1000   # сколько сообщений хранить по каждому каналу

    def render_entry(txt, entry):
        """Вставить одно сообщение в виджет с учётом текущих галочек."""
        t, name, original, translated, _src = entry
        # запоминаем строку, с которой начнётся запись — по ней ПКМ найдёт её
        start_line = int(str(txt.index("end-1c")).split(".")[0])
        entry_starts.setdefault(txt, []).append(start_line)
        # упоминание имени персонажа этого клиента (кроме его же сообщений) —
        # подсвечиваем, чтобы в шумном чате было видно «обратились ко мне»
        mention = False
        owner = txt_owner.get(txt)
        if owner is not None and name:
            cls = cfg.get("clients") or []
            if owner[0] < len(cls):
                pname = cls[owner[0]].get("name") or ""
                if pname.strip().lower() != (name or "").strip().lower():
                    mention = mentioned_in(original, pname) or \
                        mentioned_in(translated or "", pname)
        if t:
            txt.insert("end", f"[{t}] ", "time")
        if name:
            txt.insert("end", f"{name}: ", "name")
        if translated is None:
            # перевода нет — оригинал показываем в любом случае
            txt.insert("end", original + "\n", "orig")
            if tr_var.get() and original:
                txt.insert("end", tr("msg_untranslated") + "\n", "err")
            txt.insert("end", "\n")
        elif translated == original:
            # сообщение без перевода (уже на языке, языковой канал «как есть»)
            # — пустую строку-разделитель не добавляем, интервал не нужен
            txt.insert("end", original + "\n")
        elif orig_var.get():
            txt.insert("end", original + "\n", "orig")
            txt.insert("end", translated + "\n")
            txt.insert("end", "\n")
        else:
            txt.insert("end", translated + "\n")    # только перевод
            txt.insert("end", "\n")
        if mention:
            txt.tag_add("mention", f"{start_line}.0", "end-1c")

    def tab_visible(client_idx, channel):
        return (client_idx == current_client["idx"]
                and channel == active_channel(client_idx))

    def render_past_entry(txt, entry):
        """Одно сообщение «прошлого»: тот же вид, что у render_entry, но всё
        со стилем past — сразу ясно, что строка не из текущего потока."""
        t, name, original, translated, _src = entry
        if t:
            txt.insert("end", f"[{t}] ", ("time", "past"))
        if name:
            txt.insert("end", f"{name}: ", ("name", "past"))
        if translated is None:
            txt.insert("end", original + "\n", ("orig", "past"))
        elif translated == original:
            txt.insert("end", original + "\n", "past")
        elif orig_var.get():
            txt.insert("end", original + "\n", ("orig", "past"))
            txt.insert("end", translated + "\n", "past")
            txt.insert("end", "\n", "past")
        else:
            txt.insert("end", translated + "\n", "past")
            txt.insert("end", "\n", "past")

    def render_past_block(txt, block):
        """Заголовок + строки блока «прошлого» в конец виджета."""
        if block.get("header"):
            txt.insert("end", block["header"] + "\n", "past_head")
        for entry in block.get("entries") or []:
            render_past_entry(txt, entry)

    def rebuild_channel(client_idx, channel):
        if client_idx >= len(clients_ui):
            return
        txt, _ = clients_ui[client_idx]["tabs"][channel]
        # читающего в середине истории новое/перерисованное сообщение не
        # должно «выбрасывать» вниз: позиция сохраняется, если он прокручен
        # вверх; скрытые вкладки всегда остаются приколотыми к последнему
        top = txt.index("@0,0")              # первая видимая строка
        at_end = float(txt.yview()[1]) > 0.99
        txt.config(state="normal")
        txt.delete("1.0", "end")
        entry_starts[txt] = []
        block = past.get((client_idx, channel))
        if block and block.get("pos") == "start" and \
                (block.get("entries") or block.get("header")):
            render_past_block(txt, block)
        for entry in history.get((client_idx, channel), []):
            render_entry(txt, entry)
        if block and block.get("pos") == "end" and \
                (block.get("entries") or block.get("header")):
            render_past_block(txt, block)
        if at_end or not tab_visible(client_idx, channel):
            txt.see("end")
        else:
            txt.see(top)
        txt.config(state="disabled")

    # ---------------- «Порыться в прошлом» ----------------
    def collect_past_lines(client_idx, channel, mode, value):
        """Строки «прошлого» канала: байты файлов ДО границы запуска
        наблюдателя, отфильтрованные по числу (последние value) или по
        времени (value минут до запуска). Разбор — тот же parse_chat_line,
        что и для живых строк."""
        watchers = state.get("watchers") or []
        if client_idx >= len(watchers) or watchers[client_idx] is None:
            return []
        w = watchers[client_idx]
        pref = next(((p, pm) for n, p, pm in CHANNELS if n == channel), None)
        if pref is None:
            return []
        prefix, is_pm = pref
        lines = []
        for path in w.current_paths(prefix, is_pm):
            boundary = w.start_offsets.get(path)
            if not boundary:
                continue            # файла не было на момент запуска
            try:
                boundary = min(boundary, os.path.getsize(path))
                with open(path, "r", encoding="utf-8",
                          errors="replace") as f:
                    data = f.read(boundary)
            except OSError:
                continue
            batch = [ln.strip() for ln in data.splitlines() if ln.strip()]
            for msg in reassemble_lines(batch):
                if parse_chat_line(msg):
                    lines.append(msg)
        if mode == "minutes":
            threshold = w.started_at - datetime.timedelta(minutes=value)
            kept = []
            for line in lines:
                m = re.match(r"^\[(\d{2}):(\d{2}):(\d{2})\]", line)
                if not m:
                    continue
                hh, mm, ss = (int(g) for g in m.groups())
                cand = w.started_at.replace(hour=hh, minute=mm, second=ss)
                # время строки «из будущего» — это вчерашняя полночь
                if cand > w.started_at + datetime.timedelta(hours=12):
                    cand -= datetime.timedelta(days=1)
                if cand >= threshold:
                    kept.append(line)
            lines = kept
        else:
            lines = lines[-value:]
        return lines

    def dig_past(client_idx, mode, value, pos):
        """Раскопать «прошлое» ВСЕХ каналов клиента: строки каждого канала
        до запуска уходят в тот же переводчик, что и живые сообщения;
        каждый канал получает свой блок со своей раскраской в начало или
        в конец своего чата. Прошлая раскопка этого клиента снимается
        целиком, со всех каналов."""
        header = (tr("dig_header_minutes", n=value) if mode == "minutes"
                  else tr("dig_header_count", n=value))
        # один токен на всю раскопку: отсекает хвосты предыдущей, если
        # кнопку нажали снова, пока шёл перевод
        token = object()
        raw = state.get("raw")
        found = False
        for channel, _prefix, _pm in CHANNELS:
            past.pop((client_idx, channel), None)
            lines = collect_past_lines(client_idx, channel, mode, value)
            if not lines or raw is None:
                continue
            found = True
            past[(client_idx, channel)] = {
                "entries": [], "pos": pos, "header": header,
                "token": token, "shown": False}
            for line in lines:
                raw.put(("line", client_idx, channel, line, token))
        # старые блоки сняты со всех каналов — перерисовать каждую вкладку,
        # иначе снятый блок останется на экране до следующего сообщения
        for channel, _prefix, _pm in CHANNELS:
            rebuild_channel(client_idx, channel)
        if not found:
            status_var.set(tr("dig_empty"))

    def append_past_done(token, client_idx, channel, entry):
        """Очередная готовая строка «прошлого»: дополняем блок. Токен не
        совпал — опоздавший результат снятой раскопки, игнорируем."""
        key = (client_idx, channel)
        block = past.get(key)
        if block is None or block.get("token") is not token:
            return
        block["entries"].append(entry)
        if client_idx >= len(clients_ui):
            return
        txt, _ = clients_ui[client_idx]["tabs"][channel]
        if block.get("pos") == "end" and block.get("shown"):
            # конец виджета — чужие строки не трогаем, вставка дешевле
            # полной пересборки
            at_end = float(txt.yview()[1]) > 0.99
            txt.config(state="normal")
            render_past_entry(txt, entry)
            if at_end or not tab_visible(client_idx, channel):
                txt.see("end")
            txt.config(state="disabled")
        else:
            rebuild_channel(client_idx, channel)
        block["shown"] = True

    def open_dig_dialog():
        """Окно «Порыться в прошлом»: глубина (число сообщений или минут
        до запуска) и место вывода — начало или конец чатов. Копается по
        всем каналам сразу: блок в каждой вкладке из её собственного лога."""
        if not state.get("running"):
            status_var.set(tr("dig_need_run"))
            return
        ci = current_client["idx"]
        if ci >= len(clients_ui):
            return
        win = tk.Toplevel(root)
        win.title(tr("dig_title"))
        win.transient(root)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        cls = cfg.get("clients") or []
        if len(cls) > 1 and ci < len(cls):
            ttk.Label(frm, text=tr("dig_target", ch=cls[ci].get("name")
                                   or tr("client_default", n=ci + 1))
                      ).pack(anchor="w", pady=(0, 8))
        mode_var = tk.StringVar(value="count")
        pos_var = tk.StringVar(value="start")
        val_var = tk.StringVar(value="20")
        row1 = ttk.Frame(frm)
        row1.pack(anchor="w")
        ttk.Radiobutton(row1, text=tr("dig_mode_count"),
                        variable=mode_var, value="count").pack(side="left")
        ttk.Radiobutton(row1, text=tr("dig_mode_minutes"),
                        variable=mode_var,
                        value="minutes").pack(side="left", padx=(10, 0))
        row2 = ttk.Frame(frm)
        row2.pack(anchor="w", pady=(6, 0))
        val_lbl = ttk.Label(row2, text=tr("dig_label_count"))
        val_lbl.pack(side="left")
        spin = ttk.Spinbox(row2, from_=1, to=500, textvariable=val_var,
                           width=7)
        spin.pack(side="left", padx=(6, 0))

        def on_mode(*_):
            if mode_var.get() == "minutes":
                spin.config(to=10080)
                val_lbl.config(text=tr("dig_label_minutes"))
            else:
                spin.config(to=500)
                val_lbl.config(text=tr("dig_label_count"))
        mode_var.trace_add("write", on_mode)
        ttk.Label(frm, text=tr("dig_pos")).pack(anchor="w", pady=(8, 0))
        row3 = ttk.Frame(frm)
        row3.pack(anchor="w")
        ttk.Radiobutton(row3, text=tr("dig_pos_start"),
                        variable=pos_var,
                        value="start").pack(side="left")
        ttk.Radiobutton(row3, text=tr("dig_pos_end"),
                        variable=pos_var,
                        value="end").pack(side="left", padx=(10, 0))
        ttk.Label(frm, text=tr("dig_hint"), foreground="#777777",
                  wraplength=300, justify="left").pack(anchor="w", pady=(8, 0))
        row4 = ttk.Frame(frm)
        row4.pack(anchor="e", pady=(8, 0))

        def apply():
            try:
                value = int(val_var.get())
            except ValueError:
                value = 20
            mode = mode_var.get()
            value = max(1, min(value, 10080 if mode == "minutes" else 500))
            dig_past(ci, mode, value, pos_var.get())
            win.destroy()
        ttk.Button(row4, text=tr("dig_cancel"),
                   command=win.destroy).pack(side="right")
        ttk.Button(row4, text=tr("dig_show"),
                   command=apply).pack(side="right", padx=(0, 8))
        win.update_idletasks()
        x = root.winfo_rootx() + max(0, (root.winfo_width()
                                         - win.winfo_width()) // 2)
        y = root.winfo_rooty() + max(0, (root.winfo_height()
                                         - win.winfo_height()) // 3)
        win.geometry(f"+{x}+{y}")
        win.grab_set()          # модальное, как настройки
        spin.focus_set()
        win.wait_window(win)

    def refresh_view():
        """Перерисовать все вкладки (например, после переключения «Оригинал»)."""
        for i in range(len(clients_ui)):
            for ch in clients_ui[i]["tabs"]:
                rebuild_channel(i, ch)

    # переключение «Оригинал» применяется и к уже выведенной истории
    orig_var.trace_add("write", lambda *_: refresh_view())

    def append_to(client_idx, channel, time_s, name, original, translated,
                  src_lang=None):
        entry = (time_s, name, original, translated, src_lang)
        key = (client_idx, channel)
        history.setdefault(key, []).append(entry)
        overflow = len(history[key]) - HISTORY_MAX
        if overflow > 0:
            del history[key][:overflow]     # хвост истории вытеснен
            if client_idx < len(clients_ui):
                txt, _ = clients_ui[client_idx]["tabs"][channel]
                starts = entry_starts.setdefault(txt, [])
                # инкрементальное удаление срезает строки сверху виджета —
                # при блоке «прошлого» в начале он съест и его; тогда пересобираем всё
                if len(starts) >= overflow and \
                        (past.get(key) or {}).get("pos") != "start":
                    # инкрементально: удаляем из виджета только вытесненное,
                    # а не перерисовываем все 1000 записей заново
                    del starts[:overflow]
                    txt.config(state="normal")
                    if starts and starts[0] > 1:
                        first = starts[0]
                        txt.delete("1.0", f"{first}.0")
                        for i in range(len(starts)):
                            starts[i] -= first - 1
                    else:
                        txt.delete("1.0", "end")
                    txt.config(state="disabled")
                else:
                    rebuild_channel(client_idx, channel)
        elif client_idx < len(clients_ui):
            txt, _ = clients_ui[client_idx]["tabs"][channel]
            # автопрокрутка только если пользователь уже внизу; если он
            # прокручен вверх и читает старое — позицию не трогаем
            at_end = float(txt.yview()[1]) > 0.99
            txt.config(state="normal")
            render_entry(txt, entry)
            if at_end or not tab_visible(client_idx, channel):
                txt.see("end")
            txt.config(state="disabled")
        # неактивная вкладка с новым сообщением — ярко-синий заголовок
        # (красный сигнал важнее: его синим не перебиваем)
        if client_idx >= len(clients_ui):
            return
        ui = clients_ui[client_idx]
        if (client_idx != current_client["idx"]
                or channel != active_channel(client_idx)):
            if (client_idx, channel) not in alerted["tabs"]:
                ui["tab_buttons"][channel].config(fg=TAB_FG_UNREAD)
        if ui["btn"] is not None and client_idx != current_client["idx"]:
            if client_idx not in alerted["clients"]:
                ui["btn"].config(fg=TAB_FG_UNREAD)
        # сигналы: правила на подстроку в канале (звук, уведомление, красный)
        fired = match_alert_rules(cfg.get("alerts"), channel, original,
                                  translated)
        if fired:
            # в журнал — в любом случае, даже если звук/уведомление
            # выключены: красная вкладка не всегда замечается вовремя
            note0 = next((str(r.get("note")).strip() for r in fired
                          if str(r.get("note") or "").strip()), "")
            rec = {
                "at": time.strftime("%H:%M:%S"),
                "client": client_idx,
                "channel": channel,
                "text": note0 or (translated or original)[:180],
            }
            alert_log.append(rec)
            if (client_idx != current_client["idx"]
                    or channel != active_channel(client_idx)):
                alerted["tabs"].add((client_idx, channel))
                ui["tab_buttons"][channel].config(fg=TAB_FG_ALERT)
                if ui["btn"] is not None:
                    alerted["clients"].add(client_idx)
                    ui["btn"].config(fg=TAB_FG_ALERT)
            # Сигналы превыше всего: звук и уведомление срабатывают всегда,
            # когда в правиле стоит ✓ — значки каналов (🔊/🔔) на них не
            # действуют, они управляют лишь обычными звуками сообщений.
            if any(r.get("sound") for r in fired):
                alert_state["sound"] = True    # обычный звук сообщения не дублируем
                play_sound(rule_sound_path(fired, channel_sound_path(channel),
                                           cfg.get("sound_file") or ""))
                # звук пропищал — покажем, что это было
                show_last_alert(rec)
            if any(r.get("notify") for r in fired):
                cls = cfg.get("clients") or []
                label = channel
                if len(cls) > 1 and client_idx < len(cls):
                    label = (f"{cls[client_idx].get('name') or tr('client_default', n=client_idx + 1)}"
                             f" · {channel}")
                # заданный в правиле текст уведомления важнее текста сообщения
                note = next((str(r.get("note")).strip() for r in fired
                             if str(r.get("note") or "").strip()), "")
                alert_text = note or (translated or original)[:180]
                show_notification(f"{tr('alert_toast_title')} — {label}",
                                  alert_text)
                show_last_alert(rec)
                # уведомление о сигнале уже показано — обычное уведомление
                # с текстом сообщения не выдаём (двойной тост не нужен)
                return True

    def start():
        collect_cfg()
        clients = cfg.get("clients") or []
        if not clients:
            status_var.set(tr("status_add_client"))
            return
        bad = next((c for c in clients
                    if not c.get("path") or not os.path.isdir(c["path"])), None)
        if bad is not None:
            status_var.set(tr("status_bad_path", path=bad.get("path")))
            return
        stop_event.clear()
        state["raw"] = queue.Queue()
        state["done"] = queue.Queue()
        # по наблюдателю на каждую копию игры — все пишут в общую очередь,
        # каждое событие помечено индексом клиента
        state["watchers"] = []
        for i, cl in enumerate(clients):
            w = LogWatcher(cl["path"], state["raw"], stop_event,
                           client_idx=i, start_at_end=True)
            w.start()
            state["watchers"].append(w)
        state["worker"] = TranslateWorker(state["raw"], state["done"], stop_event,
                                          enabled=cfg["translate"],
                                          target_lang=target_lang, options=cfg)
        state["worker"].start()
        state["running"] = True
        run_btn.config(text=tr("btn_stop"))
        mode = tr("status_mode_tail")
        state["count"] = 0
        names = ", ".join(c.get("name") or tr("client_default", n=k + 1)
                          for k, c in enumerate(clients))
        status_var.set(tr("status_watching", n=len(clients), names=names,
                          mode=mode))
        log_debug(f"старт: клиенты={[(c.get('name'), c['path']) for c in clients]} "
                  f"target={target_lang} translate={cfg['translate']}")

    def stop():
        stop_event.set()
        state["running"] = False
        run_btn.config(text=tr("btn_start"))
        status_var.set(tr("status_stopped"))
        log_debug("стоп")

    def toggle():
        stop() if state["running"] else start()
    run_btn.config(command=toggle)

    def supervise():
        """Перезапуск фоновых потоков, если они неожиданно умерли."""
        if not state["running"] or stop_event.is_set():
            return
        clients = cfg.get("clients") or []
        for i, w in enumerate(list(state.get("watchers") or [])):
            if w is None or w.is_alive() or i >= len(clients):
                continue
            log_debug(f"надзор: наблюдатель клиента {i} умер — перезапуск")
            nw = LogWatcher(clients[i]["path"], state["raw"], stop_event,
                            client_idx=i, start_at_end=True)
            nw.start()
            state["watchers"][i] = nw
            status_var.set(tr("watcher_restarted"))
        if state["worker"] is not None and not state["worker"].is_alive():
            log_debug("надзор: переводчик умер — перезапуск")
            state["worker"] = TranslateWorker(state["raw"], state["done"], stop_event,
                                              enabled=tr_var.get(),
                                              target_lang=target_lang,
                                              options=cfg)
            state["worker"].start()
            status_var.set(tr("worker_restarted"))

    def poll():
        try:
            supervise()
            # запрос «покажи окно» от второго запущенного экземпляра
            try:
                if os.path.isfile(ACTIVATE_TRIGGER):
                    os.remove(ACTIVATE_TRIGGER)
                    log_debug("получен запрос активации от второго экземпляра")
                    _show_window()
            except Exception:
                pass
            # периодическое обновление индикатора переводчика, даже без сообщений
            now = time.monotonic()
            if state.get("running") and now - state.get("status_at", 0) > 5:
                state["status_at"] = now
                status_var.set(tr("status_running", n=state.get("count", 0),
                                  backend=translator_status_text()))
            # команды из трея (потокобезопасно: только tk-виджеты)
            try:
                while True:
                    cmd = tray_cmds.get_nowait()
                    if cmd == "show":
                        _show_window()
                    elif cmd == "quit":
                        stop()
                        collect_cfg()
                        try:
                            if tray_icon[0] is not None:
                                tray_icon[0].stop()
                        except Exception:
                            pass
                        root.destroy()
                        return
            except queue.Empty:
                pass
            while True:
                item = state["done"].get_nowait()
                if item[0] == "error":
                    status_var.set(f"Ошибка: {item[1]}")
                    continue
                if item[0] == "past_done":
                    _, token, ci2, ch2, t, name, body, translated, \
                        src_lang = item
                    append_past_done(token, ci2, ch2,
                                     (t, name, body, translated, src_lang))
                    continue
                if item[0] != "done":
                    continue
                _, client_idx, channel, t, name, body, translated, src_lang = item
                alert_state["sound"] = False
                noted = append_to(client_idx, channel, t, name, body,
                                  translated, src_lang)
                state["count"] = state.get("count", 0) + 1
                status_var.set(tr("status_running", n=state["count"],
                                  backend=translator_status_text()))
                if channel_flag(cfg.get("sound_channels"), channel) \
                        and not alert_state["sound"]:
                    play_sound(channel_sound_path(channel))
                if not noted \
                        and channel_flag(cfg.get("notify_channels"), channel):
                    cls = cfg.get("clients") or []
                    label = channel
                    if len(cls) > 1 and client_idx < len(cls):
                        label = (f"{cls[client_idx].get('name') or tr('client_default', n=client_idx + 1)}"
                                 f" · {channel}")
                    show_notification(f"Wurm — {label}",
                                      (translated or body)[:180])
        except queue.Empty:
            pass
        except Exception as exc:    # цикл опроса не должен умирать молча
            log_debug(f"poll: ошибка вывода: {exc!r}\n" + traceback.format_exc())
            status_var.set(tr("err_output", exc=repr(exc)))
        root.after(100, poll)

    tray_icon = [None]   # список, чтобы замыкание могло заменить значение
    tray_cmds = queue.Queue()   # команды из потока трея → в цикл poll (потокобезопасно)

    def tray_quit():
        tray_cmds.put("quit")

    def tray_show():
        tray_cmds.put("show")

    def _show_window():
        root.deiconify()
        root.lift()
        root.focus_force()

    def on_close():
        # Крестик — свернуть в трей, полный выход — через меню иконки.
        if HAVE_TRAY and tray_icon[0] is not None:
            root.withdraw()
        else:
            tray_quit()

    if HAVE_TRAY:
        try:
            menu = pystray.Menu(
                pystray.MenuItem(tr("tray_show"), lambda *_: tray_show(), default=True),
                pystray.MenuItem(tr("tray_quit"), lambda *_: tray_quit()),
            )
            icon = pystray.Icon("WurmChatTranslator", make_tray_icon(),
                                tr("app_title"), menu)
            tray_icon[0] = icon
            threading.Thread(target=icon.run, daemon=True).start()
        except Exception as exc:
            log_debug(f"трей недоступен: {exc!r}")
            tray_icon[0] = None

    def check_updates_silent():
        """Фоновая проверка обновлений; диалог — только если есть новее."""
        run_update_check(manual=False)

    def run_update_check(manual=False):
        """Проверка релизов на GitHub. manual=True — по кнопке в настройках:
        ответ в любом случае (ошибка сети / уже последняя / есть новее)."""
        import tkinter.messagebox as mb
        def work():
            latest = check_latest_release()
            if latest is None:
                if manual:
                    root.after(0, lambda: mb.showwarning(
                        tr("update_ask_title"), tr("update_check_failed"),
                        parent=root))
                return
            mine = parse_version(APP_VERSION)
            if mine is None or latest <= mine:
                if manual:
                    root.after(0, lambda: mb.showinfo(
                        tr("update_ask_title"),
                        tr("update_none", cur=APP_VERSION), parent=root))
                return
            root.after(0, lambda: auto_update_flow(latest, manual=manual))
        threading.Thread(target=work, daemon=True).start()

    def offer_update(latest):
        import tkinter.messagebox as mb
        newer = ".".join(str(x) for x in latest)
        if mb.askyesno(
                tr("app_title"),
                tr("update_msg", new=newer, cur=APP_VERSION),
                icon="info"):
            try:
                os.startfile(RELEASES_PAGE)
            except Exception as exc:
                log_debug(f"не удалось открыть страницу релизов: {exc!r}")

    def shutdown_for_update():
        """Аккуратный выход: потоки слежения и трей остановятся с процессом."""
        log_debug("автообновление: завершение работы для замены exe")
        stop_event.set()
        try:
            if tray_icon[0] is not None:
                tray_icon[0].stop()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

    def ask_download_update(newer):
        """Вопрос перед скачиванием: «Да» / «Нет» + галочка «не напоминать».
        Возвращает (скачать, не_напоминать). Скачивание закроет приложение,
        поэтому вопрос — явный, со своим окном (нужна галочка, messagebox
        её не умеет)."""
        res = {}
        win = tk.Toplevel(root)
        win.title(tr("update_ask_title"))
        win.transient(root)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=tr("update_ask_msg", new=newer, cur=APP_VERSION),
                  wraplength=340, justify="left").pack(anchor="w")
        skip_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text=tr("update_ask_no_remind"),
                        variable=skip_var).pack(anchor="w", pady=(8, 0))
        row = ttk.Frame(frm)
        row.pack(anchor="e", pady=(12, 0))

        def done(download):
            res["dl"] = download
            res["skip"] = bool(skip_var.get())
            win.destroy()
        ttk.Button(row, text=tr("update_no"),
                   command=lambda: done(False)).pack(side="right")
        ttk.Button(row, text=tr("update_yes"),
                   command=lambda: done(True)).pack(side="right", padx=(0, 8))
        win.update_idletasks()
        x = root.winfo_rootx() + max(0, (root.winfo_width()
                                         - win.winfo_width()) // 2)
        y = root.winfo_rooty() + max(0, (root.winfo_height()
                                         - win.winfo_height()) // 3)
        win.geometry(f"+{x}+{y}")
        win.grab_set()
        win.wait_window(win)
        return bool(res.get("dl")), bool(res.get("skip"))

    def auto_update_flow(latest, manual=False):
        """Новая версия найдена: скачать-установить-перезапустить.
        manual=True — запрос по кнопке «Проверить обновления»: полный флоу
        даже при выключенной галочке автообновления (пользователь явно
        попросил). Фоновая проверка при выключенной галочке молчит —
        без диалогов и без переходов на сайт."""
        if not getattr(sys, "frozen", False):
            offer_update(latest)
            return
        if not manual and not cfg.get("auto_update"):
            log_debug("автообновление: есть новее, но галочка выключена")
            return
        newer = ".".join(str(x) for x in latest)
        # галочка «не напоминать» от этой версии — не донимаем; для другой
        # версии спросим снова
        if cfg.get("update_skipped") == newer:
            log_debug(f"автообновление: v{newer} пропущена пользователем")
            return
        # перед скачиванием — явный вопрос: это необратимо закроет приложение
        download, dont_remind = ask_download_update(newer)
        if not download:
            if dont_remind:
                cfg["update_skipped"] = newer
                save_config(cfg)
            log_debug(f"автообновление: пользователь отказался от v{newer} "
                      f"(не напоминать: {dont_remind})")
            return
        download_and_restart(latest)

    def download_and_restart(latest):
        """Скачивание с окном прогресса, подмена exe и перезапуск. Неудача
        скачивания — понятный вопрос «повторить?», а не молчаливый уход
        на сайт GitHub."""
        import tkinter.messagebox as mb
        exe = sys.executable
        tmp = exe + ".update"
        newer = ".".join(str(x) for x in latest)
        win = tk.Toplevel(root)
        win.title(tr("update_dl_title"))
        win.transient(root)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        lbl = ttk.Label(win, text=tr("update_downloading", ver=newer),
                        padding=14, wraplength=320, justify="left")
        lbl.pack()
        win.update_idletasks()
        log_debug(f"автообновление: скачивание v{newer}")

        def work():
            ok = download_update(tmp)
            root.after(0, lambda: finish(ok))

        def finish(ok):
            if not ok:
                log_debug("автообновление: скачивание не удалось")
                try:
                    win.destroy()
                except tk.TclError:
                    pass
                if mb.askretrycancel(tr("update_ask_title"),
                                     tr("update_download_failed"),
                                     parent=root):
                    download_and_restart(latest)   # ещё одна попытка
                else:
                    offer_update(latest)    # крайний случай — страница релизов
                return
            if not spawn_update_helper(exe, tmp):
                try:
                    win.destroy()
                except tk.TclError:
                    pass
                offer_update(latest)
                return
            lbl.config(text=tr("update_restarting"))
            win.update_idletasks()
            log_debug("автообновление: помощник запущен, перезапуск")
            # помощник уже ждёт нашего выхода
            root.after(900, shutdown_for_update)

        threading.Thread(target=work, daemon=True).start()

    # ---------------- диалог настроек перевода ----------------
    def open_settings_dialog():
        """Переключатели дополнительных опций перевода. Применяются сразу:
        переводчик читает флаги из cfg на каждое сообщение."""
        win = tk.Toplevel(root)
        win.title(tr("settings_title"))
        win.transient(root)
        win.resizable(False, False)

        box = ttk.Frame(win, padding=12)
        box.pack(fill="both", expand=True)

        cur_var = tk.BooleanVar(value=bool(cfg.get("translate_currency", True)))
        copy_var = tk.BooleanVar(value=bool(cfg.get("auto_copy_reply", True)))
        upd_var = tk.BooleanVar(value=bool(cfg.get("auto_update")))
        snd_ids = list(BUILTIN_SOUNDS)
        snd_names = [tr(f"snd_{s}") for s in snd_ids]
        cur_snd = cfg.get("sound") or "beep"
        if cur_snd not in snd_ids:
            cur_snd = "beep"
        snd_var = tk.StringVar(value=snd_names[snd_ids.index(cur_snd)])

        def apply(*_args):
            cfg["translate_currency"] = bool(cur_var.get())
            cfg["auto_copy_reply"] = bool(copy_var.get())
            cfg["auto_update"] = bool(upd_var.get())
            save_config(cfg)

        def apply_sound(_e=None):
            cfg["sound"] = snd_ids[snd_names.index(snd_var.get())] \
                if snd_var.get() in snd_names else "beep"
            save_config(cfg)

        def play_selected():
            apply_sound()
            play_sound(resolve_sound_path(cfg.get("sound") or "beep",
                                          cfg.get("sound_file") or ""))

        ttk.Checkbutton(box, text=tr("opt_currency"),
                        variable=cur_var, command=apply).pack(anchor="w",
                                                              pady=2)
        ttk.Checkbutton(box, text=tr("opt_auto_copy"),
                        variable=copy_var, command=apply).pack(anchor="w",
                                                               pady=2)
        ttk.Checkbutton(box, text=tr("opt_auto_update"),
                        variable=upd_var, command=apply).pack(anchor="w",
                                                            pady=2)
        # ручная проверка: результат — сообщение (нет новее / ошибка сети)
        # или тот же вопрос про скачивание, что и при автопроверке
        ttk.Button(box, text=tr("btn_check_updates"),
                   command=lambda: run_update_check(manual=True)
                   ).pack(anchor="w", padx=(22, 0), pady=(0, 4))
        snd_row = ttk.Frame(box)
        snd_row.pack(anchor="w", pady=(6, 2), fill="x")
        ttk.Label(snd_row, text=tr("opt_sound")).pack(side="left")
        snd_combo = ttk.Combobox(snd_row, textvariable=snd_var, state="readonly",
                                 width=16, values=snd_names)
        snd_combo.pack(side="left", padx=(6, 4))
        snd_combo.bind("<<ComboboxSelected>>", apply_sound)
        ttk.Button(snd_row, text=tr("btn_play"),
                   command=play_selected).pack(side="left")
        # зеркало переключателей каналов: те же флаги, что и в контекстном
        # меню вкладки (ПКМ). Галочка — set_channel_flag + сохранение +
        # обновление пассивных значков на вкладках
        ttk.Label(box, text=tr("opt_channels")).pack(anchor="w", pady=(10, 2))
        ch_grid = ttk.Frame(box)
        ch_grid.pack(anchor="w")

        def apply_ch_flag(key, channel, var, invert=False):
            val = bool(var.get())
            set_channel_flag(cfg, key, channel, (not val) if invert else val)
            save_config(cfg)
            refresh_tab_marks()

        for col, (channel, _prefix, _pm) in enumerate(CHANNELS):
            cell = ttk.Frame(ch_grid)
            cell.grid(row=0, column=col, padx=(0 if col == 0 else 12, 0),
                      sticky="n")
            ttk.Label(cell, text=channel, font=("Segoe UI", 8, "bold")
                      ).pack(anchor="w")
            # перевод: галочка = «переводить», в конфиге храним наоборот —
            # список каналов БЕЗ перевода (по умолчанию пуст = все переводятся)
            vt = tk.BooleanVar(
                value=not channel_flag(cfg.get("no_translate_channels"),
                                       channel))
            vs = tk.BooleanVar(
                value=channel_flag(cfg.get("sound_channels"), channel))
            vn = tk.BooleanVar(
                value=channel_flag(cfg.get("notify_channels"), channel))
            ttk.Checkbutton(cell, text="🌐 " + tr("opt_ch_translate"),
                            variable=vt,
                            command=lambda ch=channel, v=vt: apply_ch_flag(
                                "no_translate_channels", ch, v, invert=True)
                            ).pack(anchor="w")
            ttk.Checkbutton(cell, text="♪ " + tr("opt_ch_sound"), variable=vs,
                            command=lambda k="sound_channels", ch=channel,
                            v=vs: apply_ch_flag(k, ch, v)
                            ).pack(anchor="w")
            ttk.Checkbutton(cell, text="✉ " + tr("opt_ch_notify"),
                            variable=vn,
                            command=lambda k="notify_channels", ch=channel,
                            v=vn: apply_ch_flag(k, ch, v)
                            ).pack(anchor="w")
        ttk.Label(box, text=tr("opt_channels_hint"),
                  foreground="#777777", wraplength=380).pack(anchor="w",
                                                             pady=(6, 0))
        ttk.Label(box, text=tr("settings_hint"),
                  foreground="#777777", wraplength=380).pack(anchor="w",
                                                             pady=(10, 0))
        ttk.Button(box, text=tr("settings_close"),
                   command=win.destroy).pack(anchor="e", pady=(10, 0))

        # модальность: ввод заблокирован для главного окна, пока открыто
        win.update_idletasks()
        win.grab_set()
        win.focus_set()
        root.wait_window(win)

    # ---------------- диалог списка клиентов ----------------
    def open_clients_dialog():
        """Просмотр и редактирование списка клиентов (папок логов).

        Каждая копия игры (Steam, сайт-клиент, …) — отдельный клиент со
        своей вкладкой. Автопоиск папок — только по явному запросу кнопкой
        «Найти всех», никогда сам по себе.
        """
        import tkinter.messagebox as mb
        win = tk.Toplevel(root)
        win.title(tr("dlg_clients_title"))
        win.geometry("640x410")
        win.transient(root)
        win.minsize(480, 260)

        tree = ttk.Treeview(win, columns=("name", "path"), show="headings",
                            height=10, selectmode="extended")
        tree.heading("name", text=tr("col_name"))
        tree.heading("path", text=tr("col_path"))
        tree.column("name", width=150, anchor="w")
        tree.column("path", width=440, anchor="w")
        tree.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        # панель правки выбранной строки: имя и путь редактируются здесь
        # (двойной щелчок по строке тоже подгружает её в поля)
        edit = ttk.Frame(win)
        edit.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(edit, text=tr("col_name")).grid(row=0, column=0, sticky="w")
        ttk.Label(edit, text=tr("col_path")).grid(row=1, column=0, sticky="w",
                                                 pady=(4, 0))
        name_var = tk.StringVar()
        path_var = tk.StringVar()
        ttk.Entry(edit, textvariable=name_var).grid(
            row=0, column=1, sticky="we", padx=(6, 0))
        path_entry = ttk.Entry(edit, textvariable=path_var)
        path_entry.grid(row=1, column=1, sticky="we", padx=(6, 0), pady=(4, 0))
        ttk.Button(edit, text=tr("btn_browse"),
                   command=lambda: pick_dir()).grid(row=1, column=2,
                                                    padx=(6, 0), pady=(4, 0))
        ttk.Button(edit, text=tr("btn_apply"),
                   command=lambda: apply_row()).grid(row=1, column=3,
                                                     padx=(6, 0), pady=(4, 0))
        edit.columnconfigure(1, weight=1)
        ttk.Label(edit, text=tr("clients_edit_hint"),
                  foreground="#777777", wraplength=560).grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

        def selected_iid():
            sel = tree.selection()
            return sel[0] if sel else None

        def load_selected(_event=None):
            iid = selected_iid()
            if iid:
                n, p = row_values(iid)
                name_var.set(n)
                path_var.set(p)

        def logs_in_folder(path):
            """Имена файлов логов Wurm в папке (<канал>.<ГГГГ-ММ>.txt)
            или None, если папку прочитать нельзя."""
            try:
                return sorted(n for n in os.listdir(path)
                              if re.search(r"\.\d{4}-\d{2}\.txt$", n))
            except OSError:
                return None

        def logs_status(path):
            """Строка-оценка папки для выборщика: сколько логов внутри."""
            names = logs_in_folder(path)
            if names is None:
                return tr("pick_status_bad"), "#cc0000"
            if not names:
                return tr("pick_status_none"), "#777777"
            shown = ", ".join(names[:3]) \
                + (", …" if len(names) > 3 else "")
            return tr("pick_status_ok", n=len(names), files=shown), "#1a7a1a"

        def browse_logs_dir(initial=""):
            """Свой выбор папки вместо родного диалога Windows: тот внутри
            папки логов рисует пустой список с туманным «нет элементов…».
            Здесь рядом видно, есть ли в папке файлы логов. Возвращает
            путь или None."""
            result = {}
            dlg = tk.Toplevel(win)
            dlg.title(tr("pick_title"))
            dlg.transient(win)
            dlg.resizable(False, False)
            box = ttk.Frame(dlg, padding=10)
            box.pack(fill="both", expand=True)
            bar = ttk.Frame(box)
            bar.pack(fill="x")
            cur_var = tk.StringVar()
            ttk.Entry(bar, textvariable=cur_var, width=52).pack(side="left")
            ttk.Button(bar, text=tr("pick_up"),
                       command=lambda: navigate(up=True)).pack(side="left",
                                                               padx=(6, 0))
            lst = tk.Listbox(box, width=64, height=14,
                             font=("Segoe UI", 9))
            lst.pack(fill="both", expand=True, pady=(8, 4))
            cur_status = ttk.Label(box, wraplength=430, justify="left")
            cur_status.pack(anchor="w")
            sel_status = ttk.Label(box, wraplength=430, justify="left")
            sel_status.pack(anchor="w", pady=(2, 0))
            row = ttk.Frame(box)
            row.pack(fill="x", pady=(8, 0))
            ttk.Button(row, text=tr("pick_choose"),
                       command=lambda: choose()).pack(side="right")
            ttk.Button(row, text=tr("dig_cancel"),
                       command=dlg.destroy).pack(side="right", padx=(0, 8))

            def show_status(label, path):
                text, color = logs_status(path)
                label.config(text=text, foreground=color)

            def navigate(path=None, up=False):
                p = cur_var.get().strip() if path is None else path
                if up:
                    p = os.path.dirname(p)
                if not os.path.isdir(p):
                    return
                cur_var.set(p)
                lst.delete(0, "end")
                try:
                    subs = sorted((n for n in os.listdir(p)
                                   if os.path.isdir(os.path.join(p, n))),
                                  key=str.casefold)
                except OSError:
                    subs = []
                for n in subs:
                    full = os.path.join(p, n)
                    mark = " ✓" if logs_in_folder(full) else ""
                    lst.insert("end", n + mark)
                show_status(cur_status, p)
                sel_status.config(text="", foreground="#777777")

            def on_select(_e=None):
                sel = lst.curselection()
                if not sel:
                    return
                name = lst.get(sel[0]).removesuffix(" ✓")
                full = os.path.join(cur_var.get().strip(), name)
                text, color = logs_status(full)
                sel_status.config(text=tr("pick_sel_preview", name=name)
                                  + " — " + text, foreground=color)

            def on_double(_e=None):
                sel = lst.curselection()
                if not sel:
                    return
                name = lst.get(sel[0]).removesuffix(" ✓")
                navigate(os.path.join(cur_var.get().strip(), name))

            def choose():
                p = cur_var.get().strip()
                if os.path.isdir(p):
                    result["path"] = p
                    dlg.destroy()

            def on_enter(_e=None):
                navigate(cur_var.get().strip())

            lst.bind("<<ListboxSelect>>", on_select)
            lst.bind("<Double-1>", on_double)
            entry = bar.winfo_children()[0]
            entry.bind("<Return>", on_enter)
            start = initial if os.path.isdir(initial) \
                else os.path.expanduser("~")
            navigate(start)
            dlg.update_idletasks()
            dlg.grab_set()
            win.wait_window(dlg)
            return result.get("path")

        def pick_dir():
            d = browse_logs_dir(path_var.get().strip())
            if d:
                path_var.set(d)

        def apply_row(_event=None):
            iid = selected_iid()
            if not iid:
                return
            tree.item(iid, values=(name_var.get().strip(),
                                   path_var.get().strip()))

        tree.bind("<<TreeviewSelect>>", load_selected)
        tree.bind("<Double-1>", load_selected)
        path_entry.bind("<Return>", apply_row)

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=8, pady=(0, 8))

        def row_values(iid):
            v = tree.item(iid, "values")
            return (v[0] if len(v) > 0 else "", v[1] if len(v) > 1 else "")

        def refill():
            for iid in tree.get_children():
                tree.delete(iid)
            for i, c in enumerate(cfg.get("clients") or []):
                tree.insert("", "end", iid=str(i),
                            values=(c.get("name") or tr("client_default", n=i + 1),
                                    c.get("path", "")))

        def add_row(name="", path=""):
            iid = str(int(time.time() * 1000) % 10**8 + len(tree.get_children()))
            tree.insert("", "end", iid=iid,
                        values=(name or tr("client_default",
                                            n=len(tree.get_children()) + 1), path))

        def find_all():
            """Автопоиск папок логов всех клиентов: Steam и сайт-клиент."""
            found = find_all_logs_dirs()
            if not found:
                mb.showinfo(tr("app_title"), tr("find_none"))
                return
            existing = {p.lower() for p in (row_values(i)[1]
                                            for i in tree.get_children())}
            added = 0
            for c in found:
                if c["path"].lower() not in existing:
                    add_row(c["name"], c["path"])
                    existing.add(c["path"].lower())
                    added += 1
            if added == 0:
                mb.showinfo(tr("app_title"), tr("find_all_present"))

        def add_manual():
            d = browse_logs_dir("")
            if not d:
                return
            name = os.path.basename(os.path.dirname(d)) or \
                tr("client_default", n=len(tree.get_children()) + 1)
            add_row(name, d)

        def remove_selected():
            for sel in tree.selection():
                tree.delete(sel)

        ttk.Button(btns, text=tr("btn_find_all"), command=find_all).pack(side="left")
        ttk.Button(btns, text=tr("btn_add"), command=add_manual).pack(side="left", padx=4)
        ttk.Button(btns, text=tr("btn_remove"), command=remove_selected).pack(side="left")
        # «Закрыть» = применить изменения и закрыть (как и крестик окна)
        ttk.Button(btns, text=tr("btn_close"),
                   command=lambda: on_close_dialog()).pack(side="right")

        def apply_and_refresh():
            cfg["clients"] = [
                {"name": row_values(iid)[0] or tr("client_default", n=k + 1),
                 "path": row_values(iid)[1]}
                for k, iid in enumerate(tree.get_children())]
            save_config(cfg)
            clients_var.set(clients_summary())
            rebuild_clients_area()
            if state.get("running"):
                # набор клиентов изменился — перезапускаем слежение
                stop()
                start()

        def on_close_dialog():
            apply_and_refresh()
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close_dialog)
        refill()

    # ---------------- диалог сигналов ----------------
    def open_alerts_dialog():
        """Правила реакции на подстроку в канале: звук, уведомление, красная
        вкладка. Проверяются оригинал и перевод сообщения, без учёта регистра.
        «Закрыть» = сохранить в конфиг."""
        win = tk.Toplevel(root)
        win.title(tr("alerts_title"))
        win.geometry("800x340")
        win.transient(root)
        win.minsize(660, 300)

        rules = [dict(r) for r in (cfg.get("alerts") or [])
                 if isinstance(r, dict)]

        table_frame = ttk.Frame(win)
        table_frame.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        tree = ttk.Treeview(table_frame,
                            columns=("on", "channel", "pattern", "sound",
                                     "notify", "note"),
                            show="headings", height=8, selectmode="browse")
        tree.heading("on", text=tr("alerts_col_on"))
        tree.heading("channel", text=tr("alerts_col_channel"))
        tree.heading("pattern", text=tr("alerts_col_pattern"))
        tree.heading("sound", text=tr("alerts_col_sound"))
        tree.heading("notify", text=tr("alerts_col_notify"))
        tree.heading("note", text=tr("alerts_col_note"))
        tree.column("on", width=40, anchor="center")
        tree.column("channel", width=130, anchor="w")
        tree.column("pattern", width=190, anchor="w")
        tree.column("sound", width=95, anchor="center")
        tree.column("notify", width=75, anchor="center")
        tree.column("note", width=190, anchor="w")
        scroll = ttk.Scrollbar(table_frame, orient="vertical",
                               command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        mark = lambda b: "✓" if b else "—"   # noqa: E731
        ch_names = [name for name, _, _ in CHANNELS]
        BOOL_COLS = {"on": "enabled", "notify": "notify"}

        def autosave():
            cfg["alerts"] = rules
            save_config(cfg)

        def channel_text(r):
            chans = rule_channels(r)
            if not chans:
                return tr("alert_any")
            return ", ".join(n for n in ch_names if n in chans)

        def sound_text(r):
            """Ячейка «Звук»: — выкл; «✓» — как у канала; имя — свой звук."""
            if not r.get("sound"):
                return "—"
            sid = r.get("sound_id") or ""
            return tr(f"snd_{sid}") if sid else "✓"

        def refill():
            for iid in tree.get_children():
                tree.delete(iid)
            for i, r in enumerate(rules):
                tree.insert("", "end", iid=str(i), values=(
                    mark(bool(r.get("enabled", True))),
                    channel_text(r),
                    r.get("pattern", ""),
                    sound_text(r),
                    mark(bool(r.get("notify"))),
                    r.get("note", "")))

        # -------- выпадающие списки прямо в ячейках («Канал», «Звук») --------
        # По щелчку под ячейкой раскрывается листбокс. Пункт применяется и
        # сохраняется сразу. Список каналов — с множественным выбором (открыт,
        # пока не кликнули мимо); список звуков — одиночный выбор, закрывается
        # сразу. Отдельных окон нет — фокус и z-order не участвуют.
        cell_dd = {"lb": None, "fresh": False}

        def close_dropdown():
            lb = cell_dd.get("lb")
            cell_dd["lb"] = None
            if lb is not None:
                try:
                    lb.destroy()
                except tk.TclError:
                    pass

        def _dd_global_click(event):
            """Клик мимо списка — закрыть его (клик дальше отрабатывает)."""
            lb = cell_dd.get("lb")
            if lb is None:
                return
            if cell_dd["fresh"]:            # собственный щелчок открытия
                cell_dd["fresh"] = False
                return
            px, py = lb.winfo_rootx(), lb.winfo_rooty()
            pw, ph = lb.winfo_width(), lb.winfo_height()
            if px <= event.x_root < px + pw and py <= event.y_root < py + ph:
                return                      # клик внутри списка
            close_dropdown()

        def _dd_global_escape(_e):
            close_dropdown()

        def _dd_pick(event):
            lb = event.widget
            idx = lb.nearest(event.y)
            if idx >= 0 and getattr(lb, "_on_pick", None):
                lb._on_pick(idx)
            return "break"                  # без стандартного выделения

        win.bind_all("<Button-1>", _dd_global_click, add="+")
        win.bind_all("<Escape>", _dd_global_escape, add="+")

        def show_dropdown(iid, col, min_width, render, on_pick):
            """Листбокс под ячейкой col: render(lb) заполняет пункты,
            on_pick(idx) — обработчик выбора. Активен один список."""
            close_dropdown()
            bbox = tree.bbox(iid, col)
            if not bbox:
                return
            x, y, w, h = bbox
            fx = x + tree.winfo_x()
            fy = y + tree.winfo_y()
            lb = tk.Listbox(table_frame, height=8, exportselection=False,
                            activestyle="none", highlightthickness=1,
                            cursor="hand2", relief="solid", bd=1)
            lb.place(x=fx, y=0, width=max(w, min_width))   # для замера высоты
            lh = lb.winfo_reqheight()
            if fy + h + lh > table_frame.winfo_height():
                ly = max(0, fy - lh)                    # раскрыть вверх
            else:
                ly = fy + h                             # раскрыть вниз
            lb.place(x=fx, y=ly, width=max(w, min_width))
            lb.bind("<Button-1>", _dd_pick)
            lb.bind("<ButtonRelease-1>", lambda _e: "break")
            lb.bind("<B1-Motion>", lambda _e: "break")
            lb._on_pick = on_pick
            render(lb)
            cell_dd["lb"] = lb
            cell_dd["fresh"] = True

        def open_channel_dropdown(iid):
            r = rules[int(iid)]
            names = [tr("alert_any")] + ch_names

            def render(lb):
                lb.delete(0, "end")
                chans = set(rule_channels(r))
                for n in names:
                    on = (not chans) if n == tr("alert_any") else (n in chans)
                    lb.insert("end", ("☑ " if on else "☐ ") + n)

            def on_pick(idx):
                name = names[idx]
                chans = set(rule_channels(r))
                if name == tr("alert_any"):
                    chans = set()               # «Любой» — сбросить каналы
                elif name in chans:
                    chans.discard(name)
                else:
                    chans.add(name)
                r.pop("channel", None)          # старый формат не нужен
                r["channels"] = [n for n in ch_names if n in chans]
                refill()
                tree.selection_set(iid)
                autosave()
                if cell_dd.get("lb") is not None:
                    render(cell_dd["lb"])       # мультивыбор — список открыт

            show_dropdown(iid, "channel", 170, render, on_pick)

        def open_sound_dropdown(iid):
            """Выбор сигнала правила: выключен / «как у канала» / конкретный
            звук из встроенной коллекции. Одиночный выбор — список закрывается."""
            r = rules[int(iid)]

            def render(lb):
                lb.delete(0, "end")
                cur = ("off",) if not r.get("sound") \
                    else ("",) if not r.get("sound_id") else (r["sound_id"],)
                items = [("off", tr("sound_off")),
                         ("", tr("sound_channel_default"))] \
                    + [(sid, tr(f"snd_{sid}")) for sid in BUILTIN_SOUNDS]
                lb._items = items
                for sid, label in items:
                    lb.insert("end", ("● " if sid in cur else "○ ") + label)

            def on_pick(idx):
                lb = cell_dd.get("lb")
                items = getattr(lb, "_items", []) if lb is not None else []
                sid = items[idx][0] if idx < len(items) else "off"
                if sid == "off":
                    r["sound"] = False
                else:
                    r["sound"] = True
                    if sid:
                        r["sound_id"] = sid
                    else:
                        r.pop("sound_id", None)
                refill()
                tree.selection_set(iid)
                autosave()
                close_dropdown()                # одиночный выбор — закрыли

            show_dropdown(iid, "sound", 160, render, on_pick)

        # -------- щелчок: переключить флажок или открыть выбор каналов --------
        def on_click(event):
            iid = tree.identify_row(event.y)
            col_id = tree.identify_column(event.x)
            if not iid or not col_id:
                return
            i = int(iid)
            if i >= len(rules):
                return
            col = tree["columns"][int(col_id[1:]) - 1]
            r = rules[i]
            if col in BOOL_COLS:
                key = BOOL_COLS[col]
                r[key] = not bool(r.get(key))
            elif col == "channel":
                open_channel_dropdown(iid)
                return      # список сам сохраняет изменения
            elif col == "sound":
                open_sound_dropdown(iid)
                return      # список сам сохраняет изменения
            else:
                return
            refill()
            tree.selection_set(str(i))
            autosave()

        tree.bind("<Button-1>", on_click)

        # -------- правка текста прямо в ячейке («Подстрока», «Текст уведомл.») --------
        TEXT_COLS = {"pattern": "pattern", "note": "note"}
        edit_state = {"widget": None, "iid": None, "key": None}

        def commit_edit(_e=None):
            wdg = edit_state["widget"]
            if wdg is None:
                return
            value = wdg.get().strip()
            iid = edit_state["iid"]
            key = edit_state["key"]
            edit_state["widget"] = None
            edit_state["iid"] = None
            edit_state["key"] = None
            # уничтожать виджет изнутри его же события нельзя — через idle
            win.after_idle(wdg.destroy)
            if iid is None or key is None:
                return
            i = int(iid)
            if i >= len(rules):
                return
            if key == "pattern":
                if value:
                    rules[i]["pattern"] = value
                    refill()
                    tree.selection_set(iid)
                # пустая подстрока — оставляем прежнее значение: опечатка
                # при правке не должна молча удалять всё правило
            else:                    # «Текст уведомл.»: пусто = без текста
                if value:
                    rules[i]["note"] = value
                else:
                    rules[i].pop("note", None)
                refill()
                tree.selection_set(iid)
            autosave()

        def cancel_edit(_e=None):
            wdg = edit_state["widget"]
            edit_state["widget"] = None
            edit_state["iid"] = None
            edit_state["key"] = None
            if wdg is not None:
                win.after_idle(wdg.destroy)

        def open_cell_editor(iid, col):
            """Поле ввода поверх ячейки «Подстрока»/«Текст уведомл.»."""
            key = TEXT_COLS[col]
            bbox = tree.bbox(iid, col)
            if not bbox:
                return
            x, y, w, h = bbox
            entry = ttk.Entry(table_frame)
            # bbox относительно tree — пересчитываем в координаты рамки
            entry.place(x=x + tree.winfo_x(), y=y + tree.winfo_y(),
                        width=w, height=h)
            entry.insert(0, rules[int(iid)].get(key, ""))
            entry.select_range(0, "end")
            edit_state.update(widget=entry, iid=iid, key=key)
            entry.bind("<Return>", commit_edit)
            entry.bind("<FocusOut>", on_focus_out)
            entry.bind("<Escape>", cancel_edit)
            # фокус через after_idle: при вызове из кнопки Tk иначе
            # возвращает фокус самой кнопке после завершения команды
            win.after_idle(entry.focus_set)

        def on_focus_out(_e=None):
            """Коммит при уходе фокуса — но не в момент фокусного танца
            Windows (сразу после focus_set фокус может вернуться)."""
            def check():
                wdg = edit_state["widget"]
                if wdg is None:
                    return
                try:
                    cur = str(win.tk.call("focus", "-displayof", win._w))
                except tk.TclError:
                    return
                if cur == wdg._w or cur.startswith(wdg._w + "."):
                    return          # фокус внутри поля — не коммитим
                commit_edit()
            win.after_idle(check)

        def start_edit(event):
            if edit_state["widget"] is not None:
                commit_edit()
            iid = tree.identify_row(event.y)
            col_id = tree.identify_column(event.x)
            if not iid or not col_id:
                return
            col = tree["columns"][int(col_id[1:]) - 1]
            if col not in TEXT_COLS:
                return
            open_cell_editor(iid, col)

        tree.bind("<Double-Button-1>", start_edit)

        def add_rule():
            if edit_state["widget"] is not None:
                commit_edit()
            rules.append({"channels": [], "pattern": "",
                          "enabled": True, "sound": True, "notify": False,
                          "note": ""})
            refill()
            iid = str(len(rules) - 1)
            tree.see(iid)
            tree.selection_set(iid)
            open_cell_editor(iid, "pattern")   # сразу вводим подстроку

        def remove_rule():
            if edit_state["widget"] is not None:
                cancel_edit()
            for sel in sorted((int(s) for s in tree.selection()), reverse=True):
                if 0 <= sel < len(rules):
                    del rules[sel]
            refill()
            autosave()

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=8, pady=(4, 8))
        ttk.Button(btns, text=tr("alerts_add"),
                   command=add_rule).pack(side="left")
        ttk.Button(btns, text=tr("alerts_remove"),
                   command=remove_rule).pack(side="left", padx=4)
        ttk.Button(btns, text=tr("btn_close"),
                   command=lambda: on_close()).pack(side="right")

        ttk.Label(win, text=tr("alerts_hint"), foreground="#777777",
                  wraplength=600).pack(anchor="w", padx=8, pady=(0, 4))

        def on_close():
            if edit_state["widget"] is not None:
                commit_edit()
            autosave()
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        refill()

    # ---------------- журнал срабатываний сигналов ----------------
    def open_alert_log_dialog():
        """Что и когда сработало: время, канал, текст. Двойной щелчок —
        перейти к вкладке, где это прилетело."""
        win = tk.Toplevel(root)
        win.title(tr("alerts_log_title"))
        win.geometry("560x320")
        win.transient(root)
        win.minsize(440, 240)

        tree = ttk.Treeview(win, columns=("time", "channel", "text"),
                            show="headings", height=12)
        tree.heading("time", text=tr("alerts_log_col_time"))
        tree.heading("channel", text=tr("alerts_log_col_channel"))
        tree.heading("text", text=tr("alerts_log_col_text"))
        tree.column("time", width=70, anchor="center")
        tree.column("channel", width=110, anchor="w")
        tree.column("text", width=330, anchor="w")
        tree.pack(fill="both", expand=True, padx=8, pady=8)

        for i, rec in enumerate(alert_log):
            tree.insert("", "end", iid=str(i), values=(
                rec["at"], rec["channel"], rec["text"][:120]))

        if not alert_log:
            ttk.Label(win, text=tr("alerts_log_empty"),
                      foreground="#777777").pack(anchor="w", padx=8)

        def jump(_event):
            sel = tree.selection()
            if not sel:
                return
            rec = alert_log[int(sel[0])]
            if rec["client"] < len(clients_ui):
                switch_client(rec["client"])
                switch_tab(rec["client"], rec["channel"])
                win.destroy()

        tree.bind("<Double-Button-1>", jump)
        ttk.Button(win, text=tr("btn_close"), command=win.destroy).pack(
            anchor="e", padx=8, pady=(0, 8))

    # ---------------- окно последнего оповещения ----------------
    # Пропищал звук — а что случилось? Небольшое окошко в правом нижнем
    # углу экрана: время, клиент, канал и текст сигнала. Само закрывается
    # через несколько секунд; новый сигнал обновляет окно и таймер.
    last_alert_ui = {"win": None, "job": None, "rec": None}

    def close_last_alert():
        last_alert_ui["job"] = None
        win = last_alert_ui["win"]
        last_alert_ui["win"] = None
        if win is not None and win.winfo_exists():
            win.destroy()

    def jump_last_alert():
        rec = last_alert_ui.get("rec")
        if rec is None or rec["client"] >= len(clients_ui):
            close_last_alert()
            return
        switch_client(rec["client"])
        switch_tab(rec["client"], rec["channel"])
        close_last_alert()

    def show_last_alert(rec):
        """rec: at/client/channel/text — создать окно или обновить в нём."""
        win = last_alert_ui["win"]
        if win is None or not win.winfo_exists():
            win = tk.Toplevel(root)
            win.title(tr("lastalert_title"))
            win.transient(root)        # без отдельной кнопки в панели задач
            win.attributes("-topmost", True)
            win.resizable(False, False)
            body = ttk.Frame(win, padding=10)
            body.pack(fill="both", expand=True)
            head = ttk.Label(body, font=("", 10, "bold"), wraplength=340)
            head.pack(anchor="w")
            text_lbl = ttk.Label(body, wraplength=340, justify="left")
            text_lbl.pack(anchor="w", pady=(6, 0))
            btns = ttk.Frame(body)
            btns.pack(anchor="e", pady=(8, 0))
            ttk.Button(btns, text=tr("lastalert_jump"),
                       command=jump_last_alert).pack(side="left")
            ttk.Button(btns, text=tr("btn_close"),
                       command=close_last_alert).pack(side="left", padx=(6, 0))
            win.update_idletasks()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            win.geometry(f"+{max(0, sw - w - 24)}+{max(0, sh - h - 72)}")
            last_alert_ui.update(win=win, head=head, text=text_lbl)
        cls = cfg.get("clients") or []
        cname = ""
        if len(cls) > 1 and rec["client"] < len(cls):
            cname = cls[rec["client"]].get("name") \
                or tr("client_default", n=rec["client"] + 1)
        label = f"{rec['at']} · {cname} · {rec['channel']}" if cname \
            else f"{rec['at']} · {rec['channel']}"
        last_alert_ui["head"].config(text=label)
        last_alert_ui["text"].config(text=rec["text"])
        last_alert_ui["rec"] = rec
        if last_alert_ui["job"] is not None:
            root.after_cancel(last_alert_ui["job"])
        last_alert_ui["job"] = root.after(15000, close_last_alert)

    def restart_gui():
        """Полный перезапуск окна: смена языка интерфейса.

        Потоки слежения останавливаются, окно закрывается; main() по флагу
        запустит run_gui заново с новым языком. Настройки к этому моменту
        уже сохранены в конфиг."""
        log_debug("перезапуск GUI (смена языка)")
        stop_event.set()
        try:
            if tray_icon[0] is not None:
                tray_icon[0].stop()
        except Exception:
            pass
        _RESTART_REQUESTED["v"] = True
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    clients_var.set(clients_summary())
    rebuild_clients_area()          # построить вкладки по списку клиентов
    root.after(100, poll)
    root.after(300, start)          # слежение запускается сразу при старте
    root.after(5000, check_updates_silent)   # проверка обновлений — в фоне
    root.mainloop()


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

def self_test():
    global CONFIG_FILE
    ok = True
    # консоль exe может быть в однобайтовой кодировке (cp1251) — не падаем
    # на не-ASCII символах в выводе самотеста
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass
    for line, expected in [
        ("[19:13:45] <Uggabuga> guards!", ("19:13:45", "Uggabuga", "guards!")),
        ("[19:10:53] <Spirit templar> I'll take care of aged troll!",
         ("19:10:53", "Spirit templar", "I'll take care of aged troll!")),
        ("[2026-10-10 17:02:17] <Liel> диоптра собралась",
         ("17:02:17", "Liel", "диоптра собралась")),
        ("[17:38:41] Uggabuga has reached 30 Longsword!",
         ("17:38:41", None, "Uggabuga has reached 30 Longsword!")),
        ("[21:42:03] PlayerOne: hello there", ("21:42:03", "PlayerOne", "hello there")),
        ("Logging started 2026-10-03", None),
        ("[17:33:11] ", None),
    ]:
        got = parse_chat_line(line)
        good = got == expected
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] parse: {line!r} -> {got}")
    # многострочные сообщения: продолжения без метки времени собираются
    # обратно в одно сообщение, а не выбрасываются
    try:
        joined = reassemble_lines([
            "[15:34:52] <Viisi> Proverbs 27:2",
            "  Let someone else praise you, and not your own mouth;",
            "  an outsider, and not your own lips.",
        ])
        good = (len(joined) == 1
                and parse_chat_line(joined[0])
                == ("15:34:52", "Viisi",
                    "Proverbs 27:2\n"
                    "Let someone else praise you, and not your own mouth;\n"
                    "an outsider, and not your own lips."))
        # однострочные сообщения идут каждое своим элементом, мусор до
        # первого сообщения по-прежнему отбрасывается
        good &= reassemble_lines(["", "[10:00:00] <A> one", "[10:00:05] <B> two"]) == [
            "[10:00:00] <A> one", "[10:00:05] <B> two"]
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] многострочные сообщения: "
              f"продолжения приклеиваются к телу")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] многострочные сообщения: {exc}")
    try:
        _tr, lang = translate_auto("good evening friends")
        if not _tr:      # сети нет / сервисы лежат — не повод ронять самотест
            print("  [WARN] translate: нет ответа от сервисов (сеть?) — "
                  "проверка результата пропущена")
        else:
            good = lang == "en" and "вечер" in _tr.lower()
            ok &= good
            backend = "mymemory" if GTX_LAST_ERROR == "429" else "gtx"
            print(f"  [{'OK ' if good else 'FAIL'}] translate ({backend}): {_tr!r} ({lang})")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] translate: {exc}")
    try:
        # явный исходный язык (панель ответа: ru → en)
        _tr2, lang2 = translate_auto("продам кедр и вемп за фавор", dst="en", src="ru")
        if not _tr2:
            print("  [WARN] translate src=ru→en: нет ответа от сервисов (сеть?) — "
                  "проверка результата пропущена")
        else:
            good = lang2 in ("ru", "auto")
            ok &= good
            print(f"  [{'OK ' if good else 'FAIL'}] translate src=ru→en: {_tr2!r} ({lang2})")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] translate src=ru: {exc}")
    try:
        got, _gl = translate_trade_body("WTS rare pickaxe, 5s")
        good = got is not None and got.startswith("Продам")
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] trade WTS: {got!r}")
        got, _gl = translate_trade_body("wtb: bulk cotton")
        good = got is not None and got.startswith("Куплю")
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] trade WTB: {got!r}")
        got, _gl = translate_trade_body("PC on a rare hammer")
        good = got is not None and got.startswith("Прайсчек")
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] trade PC: {got!r}")
        got, _gl = translate_trade_body("(Cad) WTS wemp, cotton, barley")
        good = got is not None and got.startswith("(Cad) Продам")
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] trade серверный тег: {got!r}")
        got, _gl = translate_trade_body("(Mel) wtb rare leather cap")
        good = got is not None and "(Mel) Куплю" in got
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] trade тег+регистр: {got!r}")
        got, _gl = translate_trade_body("(Xan) WTT rare sword for axe")
        good = got is not None and "(Xan) Обменяю" in got
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] trade WTT: {got!r}")
        # префиксы зависят от языка перевода (баг: всегда выходило «Куплю»)
        got, _gl = translate_trade_body("WTB rare longsword", dst="pt")
        good = got is not None and got.startswith("Compro")
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] trade WTB (pt): {got!r}")
        got, _gl = translate_trade_body("(Cad) wts deed", dst="de")
        good = got is not None and got.startswith("(Cad) Verkaufe")
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] trade wts (de): {got!r}")
        # префикс без остатка: языковой хвост «(en)» не пририсовывается
        got, _gl = translate_trade_body("WTS", dst="pt")
        good = got == "Vendo" and _gl == "en"
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] trade префикс без хвоста: {got!r}")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] trade-префиксы: {exc}")
    try:
        # сообщение из одного <Имени>: переводить нечего, хвост «(lang)» не нужен
        got, src = translate_preserving_names("<Solo>", "ru")
        good = got == "<Solo>" and src is None
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] только имя без хвоста: {got!r} ({src})")
        # обратная защита словаря (панель ответа ru→en): ru-термины в токены.
        # «продам» тоже прячется — это запись wts→продам: в ответе получим
        # живой «WTS», а не дословное «I will sell»
        prot, terms = glossary_protect_ru("продам гвозди и вемп")
        good = (len(terms) == 3
                and not re.search(r"(?<![\w])гвозди(?![\w])", prot)
                and not re.search(r"(?<![\w])вемп(?![\w])", prot))
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] словарь защита ru→en: {prot!r}")
        fake = "I sell " + terms[0][0] + " and " + terms[1][0]
        restored = glossary_restore(fake, terms)
        good = "nails" in restored and "wemp" in restored
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] словарь восстановление ru→en: "
              f"{restored!r}")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] словарь ru→en: {exc}")
    try:
        got, src = preview_translation("уже русский текст без терминов", "ru")
        good = got == "уже русский текст без терминов" and src == "ru"
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] проверка фразы (ru как есть): {got!r}")
        got, src = preview_translation("", "ru")
        good = got == "" and src is None
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] проверка фразы (пусто): {got!r}")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] проверка фразы: {exc}")
    # сигналы: матчинг подстроки по каналам, оригиналу и переводу, антиспам
    try:
        _rules = [
            {"channels": ["Local"], "pattern": "Complete the Location Details",
             "enabled": True, "sound": True, "notify": True},
            {"channels": [], "pattern": "wtb", "enabled": True},
            {"channels": ["Trade"], "pattern": "выключено",
             "enabled": False},
            {"channels": ["Local", "Village"], "pattern": "много каналов",
             "enabled": True},
            {"channel": "GL", "pattern": "наследие", "enabled": True},
        ]
        _alert_last_fired.clear()
        fired = match_alert_rules(_rules, "Local", "You complete the location details.", None)
        good = len(fired) == 1
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] сигналы: подстрока в оригинале "
              f"(регистр игнор.): {len(fired)}")
        fired = match_alert_rules(_rules, "Local", "x", "WTB rare sword")
        good = len(fired) == 1          # по переводу, канал «Любой»
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] сигналы: по переводу, любой канал: {len(fired)}")
        _alert_last_fired.clear()       # чтобы антиспам не влиял на проверку
        fired = match_alert_rules(_rules, "Village", "wtb", None)
        good = len(fired) == 1          # «wtb» на любой канал — сработает и в Village
        fired2 = match_alert_rules(_rules, "Village", "complete the location", None)
        good = good and not fired2      # …а правило Local — только для Local
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] сигналы: фильтр по каналу")
        fired = match_alert_rules(_rules, "Village", "есть много каналов тут", None)
        fired2 = match_alert_rules(_rules, "Local", "много каналов", None)
        good = len(fired) == 1 and len(fired2) == 1   # несколько каналов
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] сигналы: несколько каналов "
              f"в одном правиле")
        fired = match_alert_rules(_rules, "GL", "наследие старого формата", None)
        good = len(fired) == 1          # старый формат «channel»: строка
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] сигналы: обратная "
              f"совместимость (channel: строка)")
        fired = match_alert_rules(_rules, "Trade", "выключено правило", None)
        good = not fired
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] сигналы: выключенное правило")
        _alert_last_fired.clear()
        fired = match_alert_rules(_rules, "Local", "complete the location details", None)
        fired_again = match_alert_rules(_rules, "Local", "complete the location details", None)
        good = len(fired) == 1 and not fired_again   # антиспам
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] сигналы: антиспам (не чаще раза "
              f"в {ALERT_THROTTLE_SECONDS:.0f} с)")
        # одинаковая подстрока в двух правилах: окна антиспама — у каждого своё
        _dup = [
            {"channels": ["Local"], "pattern": "complete the location details",
             "enabled": True},
            {"channels": ["Event"], "pattern": "complete the location details",
             "enabled": True},
        ]
        _f1 = match_alert_rules(_dup, "Local", "complete the location details", None)
        _f2 = match_alert_rules(_dup, "Event", "complete the location details", None)
        good = len(_f1) == 1 and len(_f2) == 1
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] сигналы: антиспам по правилу, "
              f"не по подстроке")
        good = (len(DEFAULTS["alerts"]) == 1
                and DEFAULTS["alerts"][0].get("channels") == ["Event"]
                and DEFAULTS["alerts"][0].get("pattern")
                    == "complete the location details"
                and DEFAULTS["alerts"][0].get("note")
                and DEFAULTS["alerts"][0].get("enabled")
                and DEFAULTS["alerts"][0].get("sound")
                and DEFAULTS["alerts"][0].get("notify"))
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] сигналы: дефолтный сигнал "
              f"археологии (Event): {len(DEFAULTS['alerts'])}")
        # разрешения каналов на звук/уведомления
        c = {}
        good = not channel_flag(None, "Local") \
            and not channel_flag([], "Trade")
        set_channel_flag(c, "sound_channels", "Trade", True)
        good = good and channel_flag(c["sound_channels"], "Trade") \
            and not channel_flag(c["sound_channels"], "Local")
        set_channel_flag(c, "sound_channels", "Trade", False)
        good = good and not channel_flag(c["sound_channels"], "Trade")
        set_channel_flag(c, "notify_channels", "Event", True)
        good = good and channel_flag(c["notify_channels"], "Event") \
            and not channel_flag(c["notify_channels"], "Village")
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] сигналы: разрешения каналов "
              f"(звук/уведомления)")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] сигналы: {exc}")
    persona = steam_persona_name()
    auto = auto_logs_dir()
    good = os.path.isdir(auto)
    ok &= good
    print(f"  [{'OK ' if good else 'FAIL'}] steam-пользователь: {persona!r}; "
          f"логи: {auto}")
    try:
        entries = glossary_entries()
        created = os.path.isfile(GLOSSARY_FILE)
        good = created and len(entries) == len(DEFAULT_GLOSSARY)
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] словарь: {len(entries)} записей, "
              f"файл: {GLOSSARY_FILE}")
        cases = [
            ("Продается deed, содержание 20 шиллингов", "поселение"),
            ("анаконда рядом с guard tower, будь осторожен", "башня стражи"),
            ("wemp и хлопок оптом", "вемп"),
            ("Glimmersteel кусок 95ql", "глимерсталь"),
            ("Дух spirit templar позаботился о тролле", "храмовник"),
            ("нужна favour за каст", "фавор"),
        ]
        for src, want in cases:
            got = glossary_fix(src, "ru")
            good = want in got
            ok &= good
            print(f"  [{'OK ' if good else 'FAIL'}] словарь ru: {src!r} -> {got!r}")
        got = glossary_fix("продам кедр и вемп за фавор", "en")
        good = all(x in got for x in ("cedarwood", "wemp", "favour"))
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] словарь en: {got!r}")
        # сортированный кэш живёт по mtime: внешняя правка файла подхватывается
        _raw = glossary_raw_items()
        glossary_save_items(_raw + [{"en": "zzzcacheprobe", "ru": "тесткэша"}])
        _seen = [e[0] for e in _glossary_sorted_entries()]
        good = "zzzcacheprobe" in _seen
        glossary_save_items(_raw)          # возвращаем словарь как был
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] словарь: кэш инвалидируется "
              f"по mtime файла")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] словарь: {exc}")
    # валюта: защита токенами и восстановление словами на целевом языке
    try:
        cases = [
            ("WTS rare sword 20s", "ru", "20 серебра"),
            ("WTS rare sword 20s", "en", "20 silver"),
            ("price 2g 50s 30c 40i", "ru", "2 золота, 50 серебра, 30 меди, 40 железа"),
            ("price 2g 50s 30c 40i", "de", "2 Gold, 50 Silber, 30 Kupfer, 40 Eisen"),
            ("1g", "ru", "1 золото"),
            ("11s", "ru", "11 серебра"),
            ("21i", "ru", "21 железо"),
            ("20 seconds", "ru", "20 seconds"),   # «20s» внутри слова не трогаем
        ]
        for phrase, lang, want in cases:
            prot, amounts = currency_protect(phrase)
            got = currency_restore(prot, amounts, lang)
            good = want in got
            ok &= good
            print(f"  [{'OK ' if good else 'FAIL'}] валюта {lang}: {phrase!r} -> {got!r}")
        # сквозная проверка через живой перевод: токен выдерживает Google
        prot, amounts = currency_protect("WTS rare sword 20s")
        _live, _l = translate_preserving_names(prot, "ru")
        if not _live:
            print("  [WARN] валюта (live gtx): нет ответа от сервисов (сеть?) — "
                  "проверка результата пропущена")
        else:
            got = currency_restore(_live, amounts, "ru")
            good = "20 серебра" in got
            ok &= good
            print(f"  [{'OK ' if good else 'FAIL'}] валюта (live gtx): {got!r}")
        # опция «как есть»: токен возвращается в исходном виде
        prot, amounts = currency_protect("price 2g 50s 30c 40i")
        got = currency_restore(prot, amounts, None)
        good = "2g, 50s, 30c, 40i" in got
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] валюта как есть: {got!r}")
        # словарь: термины прячутся до перевода и возвращаются после;
        # слова с тем же корнем («nailsmith») не трогаем; торговые префиксы
        # (wts/wtb/wtt/pc) НЕ прячем — их должен видеть translate_trade_body
        prot, terms = glossary_protect("wts nails for nailsmith", "ru")
        good = (terms and not re.search(r"(?<![\w])nails(?![\w])", prot,
                                        re.IGNORECASE)
                and "nailsmith" in prot
                and bool(re.search(r"(?<![\w])wts(?![\w])", prot, re.IGNORECASE)))
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] словарь защита: {prot!r}")
        fake = prot.replace(terms[0][0], "гвозди") if terms else prot
        got = glossary_restore(fake, terms)
        good = got.startswith("wts гвозди")
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] словарь восстановление: {got!r}")
        # языковые каналы: детектор «… GL» по названию языка
        good = (bool(RE_LANG_CHANNEL.match("Russian GL"))
                and bool(RE_LANG_CHANNEL.match("english gl"))
                and not RE_LANG_CHANNEL.match("GL-Freedom")
                and not RE_LANG_CHANNEL.match("Local"))
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] языковые каналы: Russian GL / "
              "english gl — да; GL-Freedom / Local — нет")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] валюта: {exc}")
    # выключенный перевод канала: worker отдаёт оригинал как есть, без
    # обращения к сети (так рендерится без пустой строки-разделителя)
    try:
        w = TranslateWorker(queue.Queue(), queue.Queue(),
                            threading.Event(), enabled=True, target_lang="ru",
                            options={"no_translate_channels": ["Event"]})
        w.raw.put(("line", 0, "Event", "[12:00:00] <Kimi> guards!"))
        w._process_one()
        item = w.done.get(timeout=1)
        # ("done", клиент, канал, время, имя, оригинал, перевод, src_lang)
        good = (item[0] == "done" and item[5] == "guards!"
                and item[6] == "guards!" and item[7] is None)
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] канал без перевода: "
              "оригинал проходит как есть")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] канал без перевода: {exc}")
    # миграция: национальные чаты в no_translate_channels по умолчанию;
    # старый флаг translate_lang_channels=True уважаем как явное «вкл»
    try:
        _cfg_bak = CONFIG_FILE
        cases = [(None, True), (False, True), (True, False)]
        good = True
        for old_flag, expect_gl in cases:
            p = os.path.join(tempfile.mkdtemp(), "cfg.json")
            data = {"clients": []}
            if old_flag is not None:
                data["translate_lang_channels"] = old_flag
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f)
            CONFIG_FILE = p
            c = load_config()
            has = "Russian GL" in (c.get("no_translate_channels") or [])
            good = good and (has == expect_gl)
        CONFIG_FILE = _cfg_bak
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] миграция языковых чатов: "
              "по умолчанию выкл; старый флаг вкл — уважаем")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] миграция языковых чатов: {exc}")
    # встроенные звуки: файлы на месте, все id резолвятся; свой файл важнее
    try:
        good = sounds_dir() is not None
        for sid in BUILTIN_SOUNDS:
            good = good and resolve_sound_path(sid) is not None
        good = good and resolve_sound_path("no_such_sound") is None
        # свой wav важнее встроенного
        _custom = os.path.join(tempfile.gettempdir(), "wurm_probe.wav")
        with open(resolve_sound_path("beep"), "rb") as src, \
                open(_custom, "wb") as dst:
            dst.write(src.read())
        good = good and resolve_sound_path("beep", custom=_custom) == _custom
        os.remove(_custom)
        # звук правила сигнала: свой выбор важнее звука канала
        _ch = os.path.join(tempfile.gettempdir(), "chan.wav")
        good = good and \
            rule_sound_path([{"sound": True, "sound_id": "bell"}]
                            ).endswith("bell.wav") \
            and rule_sound_path([{"sound": True}], _ch) == _ch \
            and rule_sound_path([{"sound": True, "sound_id": "zzz"}],
                                _ch) == _ch \
            and rule_sound_path([], _ch) == _ch
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] звуки: {len(BUILTIN_SOUNDS)} "
              f"встроенных, папка: {sounds_dir()}")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] звуки: {exc}")
    # упоминание имени: границы слова и регистр
    try:
        good = (mentioned_in("Uggabuga, come here!", "Uggabuga")
                and mentioned_in("uggabuga приди", "Uggabuga")
                and not mentioned_in("UggabugaSecond хай", "Uggabuga")
                and not mentioned_in("привет всем", "Uggabuga")
                and not mentioned_in("ку-ку", "")
                and not mentioned_in(None, "Uggabuga"))
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] упоминание имени: границы "
              f"слова, регистр, пустые значения")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] упоминание имени: {exc}")
    # скрипт помощника автообновления: пути экранированы, все шаги на месте
    try:
        s = build_update_script(r"C:\My Apps\WCT.exe",
                                r"C:\My Apps\WCT.exe.update", 1234)
        good = ("Wait-Process -Id 1234" in s
                and "Move-Item -LiteralPath 'C:\\My Apps\\WCT.exe' "
                    "-Destination 'C:\\My Apps\\WCT.exe.bak'" in s
                and "'C:\\My Apps\\WCT.exe.update' -Destination "
                    "'C:\\My Apps\\WCT.exe'" in s
                and "Start-Process 'C:\\My Apps\\WCT.exe'" in s
                and "catch" in s)
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] автообновление: скрипт "
              f"помощника — ожидание, замена, запуск, откат")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] автообновление: {exc}")
    # i18n: у всех языков полный набор ключей, названия — на родных языках
    try:
        ref = set(I18N["ru"])
        for code, name in LANGUAGES:
            if code == "auto":
                continue
            keys = set(I18N.get(code, {}))
            missing = ref - keys
            extra = keys - ref
            good = not missing and not extra
            ok &= good
            print(f"  [{'OK ' if good else 'FAIL'}] i18n {code} ({name}): "
                  f"ключей {len(keys)}/{len(ref)}"
                  + (f", нет: {sorted(missing)}" if missing else "")
                  + (f", лишние: {sorted(extra)}" if extra else ""))
        for code, name in LANGUAGES[1:]:
            global UI_LANG
            UI_LANG = code
            s = tr("btn_stop")
            good = s and s != "btn_stop"
            ok &= good
        UI_LANG = "ru"
        # автоопределение: auto → из Steam (или ru), явный код — без изменений
        good = resolve_target_lang("pt") == "pt" and \
            resolve_target_lang("auto") in SUPPORTED_LANGS
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] i18n resolve: "
              f"auto→{resolve_target_lang('auto')}, pt→{resolve_target_lang('pt')}")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] i18n: {exc}")
    print("Итог:", "все проверки пройдены" if ok else "есть ошибки")
    return 0 if ok else 1


def main():
    import argparse
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--test", action="store_true", help="самопроверка и выход")
    args = parser.parse_args()
    if args.test:
        raise SystemExit(self_test())
    log_debug("=== запуск GUI ===")

    # Один экземпляр: если приложение уже работает — спросить, что делать.
    # Язык этого диалога — по настройке/Steam, чтобы непонятная ситуация
    # объяснялась на языке пользователя.
    global UI_LANG
    UI_LANG = resolve_ui_lang(load_config().get("target_lang"))
    mutex_handle, already = _single_instance_mutex()
    if already:
        log_debug("обнаружен уже запущенный экземпляр")
        import tkinter as tk
        from tkinter import messagebox
        ask = tk.Tk()
        ask.withdraw()
        ask.attributes("-topmost", True)
        choice = messagebox.askyesnocancel(
            tr("app_title"),
            tr("instance_msg"))
        ask.destroy()
        if choice is None:          # Отмена
            log_debug("второй экземпляр: отмена пользователем")
            return
        if choice:                  # Да — активировать существующее
            _activate_existing()
            log_debug("второй экземпляр: запрошена активация первого")
            return
        # Нет — завершить старый экземпляр и запуститься самим
        _terminate_existing()
        log_debug("второй экземпляр: старый завершён, запускаюсь")
        time.sleep(1)

    try:
        while True:             # смена языка интерфейса = новый круг
            _RESTART_REQUESTED["v"] = False
            run_gui()
            if not _RESTART_REQUESTED["v"]:
                break
    except Exception:
        # Фатальная ошибка mainloop — фиксируем, чтобы было что показать.
        log_debug("ФАТАЛЬНАЯ ОШИБКА GUI:\n" + traceback.format_exc())
        raise
    finally:
        log_debug("=== выход ===")


if __name__ == "__main__":
    main()
