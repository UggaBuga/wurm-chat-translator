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
APP_VERSION = "1.7.1"
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
RE_NAME = re.compile(r"^\[(?:\d{4}-\d{2}-\d{2} )?(\d{2}:\d{2}:\d{2})\]\s*<([^<>\r\n]+)>\s*(.*\S)\s*$")
RE_SYS = re.compile(r"^\[(?:\d{4}-\d{2}-\d{2} )?(\d{2}:\d{2}:\d{2})\]\s*(.+?\S)\s*$")
RE_COLON = re.compile(r"^\[(?:\d{4}-\d{2}-\d{2} )?(\d{2}:\d{2}:\d{2})\]\s*([^\s:\[\]]{2,30}):\s*(.*\S)\s*$")

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


# --------------------------------------------------------------------------- #
# Звук и уведомления (из оригинального скрипта)
# --------------------------------------------------------------------------- #

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
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            else:
                print("\a", end="", flush=True)
    except Exception:
        pass


def show_notification(title, message):
    title = xml.sax.saxutils.escape(title)
    message = xml.sax.saxutils.escape(message)
    system = platform.system()
    try:
        if system == "Windows":
            ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
$xml = @\\"
<toast><visual><binding template="ToastGeneric">
    <text>{title}</text>
    <text>{message}</text>
</binding></visual></toast>
\\"@
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("WurmChatGUI").Show($toast)
"""
            creationflags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags = subprocess.CREATE_NO_WINDOW  # не мигать консолью
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=creationflags)
        elif system == "Darwin":
            subprocess.Popen(["osascript", "-e",
                              f'display notification "{message}" with title "{title}"'])
        else:
            try:
                subprocess.Popen(["notify-send", title, message])
            except FileNotFoundError:
                pass
    except Exception:
        pass


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
                    if path not in self.offsets:
                        # Новый файл, появившийся во время работы (переписка
                        # ЛС, лог нового месяца) — читаем с начала.
                        self.offsets[path] = 0
                        self.pending[path] = ""
                    if os.path.getsize(path) < self.offsets[path]:
                        self.offsets[path] = 0   # файл перезаписан
                        self.pending[path] = ""
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(self.offsets[path])
                        new_data = f.read()
                        if new_data:
                            self.offsets[path] = f.tell()
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
                for line in complete:
                    line = line.strip()
                    if line and parse_chat_line(line):
                        self.out.put(("line", self.client_idx, name, line))


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
        return f"{head} (en)", "en"
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

_glossary_sorted = {"entries": None}


def _glossary_sorted_entries():
    """Записи словаря, отсортированные по длине en (длинные — первыми:
    «guard tower» должен матчиться раньше, чем «guard»)."""
    entries = glossary_entries()
    if _glossary_sorted["entries"] is not None:
        return _glossary_sorted["entries"]
    ordered = sorted(entries, key=lambda e: -len(e[0]))
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
    _glossary_sorted["entries"] = None


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
        _, client_idx, channel, line = item

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
        # языковые каналы («Russian GL») — по умолчанию вообще без перевода
        lang_channel = bool(RE_LANG_CHANNEL.match(channel or ""))
        keep_as_is = lang_channel and not opts.get("translate_lang_channels", False)
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

        self.done.put(("done", client_idx, channel, t, name, body, translated,
                       src_lang))


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #

DEFAULTS = {
    "clients": [],           # список {"name", "path"} — по клиенту на копию игры
    "translate": True,
    "sound": False,
    "notify": False,
    "on_top": False,
    "show_original": False,
    "sound_file": "",
    "target_lang": "auto",   # auto → определить по языку клиента Steam
    "reply_src": "",         # "" → как основной язык перевода; иначе явный код
    "reply_dst": "en",       # панель ответа: на какой язык переводить
    "translate_lang_channels": False,  # переводить языковые чаты («Russian GL»)
    "translate_currency": True,        # «20s» → «20 серебра»; выкл — как есть
    "auto_copy_reply": True,           # перевод ответа — сразу в буфер обмена
}


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
        "settings_title": "Настройки перевода",
        "opt_lang_channels": "Переводить языковые чаты (Russian GL, English GL, …)",
        "opt_currency": "Переводить денежные единицы (20s → 20 серебра)",
        "opt_auto_copy": "Автоматически копировать перевод ответа в буфер обмена",
        "settings_hint": "Изменения применяются сразу, перезапуск не нужен.",
        "settings_close": "Закрыть",
        "clients_none": "не заданы",
        "lang_label": "Язык:",
        "chk_translate": "Перевод на {lang}",
        "chk_sound": "Звук",
        "chk_notify": "Уведомления",
        "chk_original": "Оригинал",
        "chk_on_top": "Поверх окон",
        "reply_label": "Ответ:",
        "ctx_reply": "Ответить",
        "btn_start": "▶ Старт",
        "btn_stop": "■ Стоп",
        "btn_clear": "Очистить всё",
        "btn_search": "🔍 Найти",
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
    },
    "en": {
        "app_title": "Wurm Chat Translator",
        "clients_label": "Clients:",
        "clients_btn": "Clients…",
        "btn_settings": "Settings…",
        "settings_title": "Translation settings",
        "opt_lang_channels": "Translate language channels (Russian GL, English GL, …)",
        "opt_auto_copy": "Automatically copy the reply translation to the clipboard",
        "opt_currency": "Translate currency units (20s → 20 silver)",
        "settings_hint": "Changes apply immediately, no restart needed.",
        "settings_close": "Close",
        "clients_none": "not set",
        "lang_label": "Language:",
        "chk_translate": "Translate to {lang}",
        "chk_sound": "Sound",
        "chk_notify": "Notifications",
        "chk_original": "Original",
        "chk_on_top": "Always on top",
        "reply_label": "Reply:",
        "ctx_reply": "Reply",
        "btn_start": "▶ Start",
        "btn_stop": "■ Stop",
        "btn_clear": "Clear all",
        "btn_search": "🔍 Find",
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
    },
    "pt": {
        "app_title": "Wurm Chat — Tradutor",
        "clients_label": "Clientes:",
        "clients_btn": "Clientes…",
        "btn_settings": "Configurações…",
        "settings_title": "Configurações de tradução",
        "opt_auto_copy": "Copiar automaticamente a tradução da resposta para a área de transferência",
        "opt_lang_channels": "Traduzir chats de idiomas (Russian GL, English GL, …)",
        "opt_currency": "Traduzir unidades monetárias (20s → 20 prata)",
        "settings_hint": "As alterações são aplicadas imediatamente, sem reiniciar.",
        "settings_close": "Fechar",
        "clients_none": "não definidos",
        "lang_label": "Idioma:",
        "chk_translate": "Traduzir para {lang}",
        "chk_sound": "Som",
        "chk_notify": "Notificações",
        "chk_original": "Original",
        "chk_on_top": "Sempre visível",
        "reply_label": "Responder:",
        "ctx_reply": "Responder",
        "btn_start": "▶ Iniciar",
        "btn_stop": "■ Parar",
        "btn_clear": "Limpar tudo",
        "btn_search": "🔍 Buscar",
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
    },
    "de": {
        "app_title": "Wurm Chat — Übersetzer",
        "clients_label": "Clients:",
        "clients_btn": "Clients…",
        "btn_settings": "Einstellungen…",
        "opt_auto_copy": "Übersetzte Antwort automatisch in die Zwischenablage kopieren",
        "settings_title": "Übersetzungseinstellungen",
        "opt_lang_channels": "Sprach-Chats übersetzen (Russian GL, English GL, …)",
        "opt_currency": "Währungseinheiten übersetzen (20s → 20 Silber)",
        "settings_hint": "Änderungen werden sofort übernommen, Neustart nicht erforderlich.",
        "settings_close": "Schließen",
        "clients_none": "nicht gesetzt",
        "lang_label": "Sprache:",
        "chk_translate": "Übersetzen auf {lang}",
        "chk_sound": "Ton",
        "chk_notify": "Benachrichtigungen",
        "chk_original": "Original",
        "chk_on_top": "Immer im Vordergrund",
        "reply_label": "Antwort:",
        "ctx_reply": "Antworten",
        "btn_start": "▶ Start",
        "btn_stop": "■ Stopp",
        "btn_clear": "Alles löschen",
        "btn_search": "🔍 Suchen",
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
    },
    "es": {
        "app_title": "Wurm Chat — Traductor",
        "clients_label": "Clientes:",
        "clients_btn": "Clientes…",
        "opt_auto_copy": "Copiar automáticamente la traducción de la respuesta al portapapeles",
        "btn_settings": "Ajustes…",
        "settings_title": "Ajustes de traducción",
        "opt_lang_channels": "Traducir chats de idiomas (Russian GL, English GL, …)",
        "opt_currency": "Traducir unidades monetarias (20s → 20 plata)",
        "settings_hint": "Los cambios se aplican inmediatamente, sin reiniciar.",
        "settings_close": "Cerrar",
        "clients_none": "sin definir",
        "lang_label": "Idioma:",
        "chk_translate": "Traducir a {lang}",
        "chk_sound": "Sonido",
        "chk_notify": "Notificaciones",
        "chk_original": "Original",
        "chk_on_top": "Siempre visible",
        "reply_label": "Responder:",
        "ctx_reply": "Responder",
        "btn_start": "▶ Iniciar",
        "btn_stop": "■ Detener",
        "btn_clear": "Borrar todo",
        "btn_search": "🔍 Buscar",
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
    },
    "fr": {
        "app_title": "Wurm Chat — Traducteur",
        "clients_label": "Clients :",
        "opt_auto_copy": "Copier automatiquement la traduction de la réponse dans le presse-papiers",
        "clients_btn": "Clients…",
        "btn_settings": "Paramètres…",
        "settings_title": "Paramètres de traduction",
        "opt_lang_channels": "Traduire les canaux de langue (Russian GL, English GL, …)",
        "opt_currency": "Traduire les unités monétaires (20s → 20 argent)",
        "settings_hint": "Les changements s'appliquent immédiatement, sans redémarrage.",
        "settings_close": "Fermer",
        "clients_none": "non définis",
        "lang_label": "Langue :",
        "chk_translate": "Traduire en {lang}",
        "chk_sound": "Son",
        "chk_notify": "Notifications",
        "chk_original": "Original",
        "chk_on_top": "Toujours au premier plan",
        "reply_label": "Réponse :",
        "ctx_reply": "Répondre",
        "btn_start": "▶ Démarrer",
        "btn_stop": "■ Arrêter",
        "btn_clear": "Tout effacer",
        "btn_search": "🔍 Chercher",
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
    },
    "zh": {
        "app_title": "Wurm 聊天翻译器",
        "opt_auto_copy": "自动将回复翻译复制到剪贴板",
        "clients_label": "客户端：",
        "clients_btn": "客户端…",
        "btn_settings": "设置…",
        "settings_title": "翻译设置",
        "opt_lang_channels": "翻译语言频道（Russian GL、English GL 等）",
        "opt_currency": "翻译货币单位（20s → 20 银）",
        "settings_hint": "更改立即生效，无需重启。",
        "settings_close": "关闭",
        "clients_none": "未设置",
        "lang_label": "语言：",
        "chk_translate": "翻译为{lang}",
        "chk_sound": "声音",
        "chk_notify": "通知",
        "chk_original": "原文",
        "chk_on_top": "置顶",
        "reply_label": "回复：",
        "ctx_reply": "回复",
        "btn_start": "▶ 开始",
        "btn_stop": "■ 停止",
        "btn_clear": "全部清除",
        "btn_search": "🔍 搜索",
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
    from tkinter import filedialog, scrolledtext, ttk

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
    tr_var = tk.BooleanVar(value=cfg["translate"])
    ttk.Checkbutton(flags, text=tr("chk_translate",
                                    lang=LANG_OBJ[UI_LANG][target_lang]),
                    variable=tr_var).pack(side="left")
    snd_var = tk.BooleanVar(value=cfg["sound"])
    ttk.Checkbutton(flags, text=tr("chk_sound"),
                    variable=snd_var).pack(side="left", padx=8)
    ntf_var = tk.BooleanVar(value=cfg["notify"])
    ttk.Checkbutton(flags, text=tr("chk_notify"),
                    variable=ntf_var).pack(side="left")
    orig_var = tk.BooleanVar(value=cfg["show_original"])
    ttk.Checkbutton(flags, text=tr("chk_original"),
                    variable=orig_var).pack(side="left", padx=8)
    top_var = tk.BooleanVar(value=cfg["on_top"])
    top_chk = ttk.Checkbutton(flags, text=tr("chk_on_top"), variable=top_var)
    top_chk.pack(side="left")
    root.attributes("-topmost", cfg["on_top"])
    top_chk.config(command=lambda: root.attributes("-topmost", top_var.get()))

    # ---------------- вкладки-окна каналов ----------------
    # Два уровня вкладок: верхний — клиенты (по одному на каждую копию игры,
    # для мультиоконной игры), внутри каждого — привычные вкладки каналов.
    # Классические tk.Label: у ttk.Notebook нельзя перекрасить вкладку
    # в ярко-синий цвет «непрочитано».
    TAB_FG = "#404040"          # обычный цвет заголовка вкладки
    TAB_FG_UNREAD = "#0066ff"   # ярко-синий — новые сообщения
    TAB_BG_ACTIVE = "#cfe3f5"   # фон активной вкладки

    clients_holder = tk.Frame(root)
    clients_holder.pack(fill="both", expand=True, padx=8, pady=4)

    clients_ui = []            # по индексу клиента: его вкладки и окна каналов
    current_client = {"idx": 0}
    history = {}               # (индекс клиента, канал) -> список сообщений
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
        # открыли вкладку клиента — признак «непрочитано» снимается
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
        for n, b in ui["tab_buttons"].items():
            if n == name:
                b.config(bg=TAB_BG_ACTIVE, relief="sunken")
                b.config(fg=TAB_FG)   # открыли вкладку — «непрочитано» снято
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
            content = ttk.Frame(cframe)
            content.pack(fill="both", expand=True, pady=(2, 0))
            tabs = {}
            tab_buttons = {}
            for name, _, _ in CHANNELS:
                frame = ttk.Frame(content)
                txt = scrolledtext.ScrolledText(frame, wrap="word", state="disabled",
                                                font=("Segoe UI", 10))
                txt.pack(fill="both", expand=True)
                txt.tag_config("time", foreground="#005a9e")
                txt.tag_config("name", font=("Segoe UI", 10, "bold"))
                txt.tag_config("orig", foreground="#777777")
                txt.tag_config("err", foreground="#cc0000")
                bind_copy(txt)
                txt.bind("<Button-3>", lambda e, idx=i, ch=name:
                         chat_context_menu(e, idx, ch))
                # фреймы каналов накладываются друг на друга (place, НЕ pack —
                # pack разложил бы их полосами), видимый — через lift()
                frame.place(relx=0, rely=0, relwidth=1, relheight=1)
                tabs[name] = (txt, frame)
                btn = tk.Label(tabbar, text=name, fg=TAB_FG, padx=8, pady=3,
                               cursor="hand2", font=("Segoe UI", 9))
                btn.pack(side="left")
                btn.bind("<Button-1>",
                         lambda _e, n=name, idx=i: switch_tab(idx, n))
                tab_buttons[name] = btn
            clients_ui.append({"btn": cbtn, "frame": cframe, "tabs": tabs,
                               "tab_buttons": tab_buttons,
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
            if src == dst:
                result = text        # языки совпали — переводить нечего
            else:
                result, _lang = translate_auto(text, dst=dst, src=src)
            if result:
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
    status_var = tk.StringVar(value=tr("status_stopped"))
    ttk.Label(bottom, textvariable=status_var).pack(side="left")
    # номер версии — тихий уголок справа; рядом неприметная ссылка на словарь.
    # Словарь — механизм пост-обработки только для русского направления,
    # поэтому ссылку показываем лишь когда перевод идёт на русский.
    ttk.Label(bottom, text=f"v{APP_VERSION}",
              foreground="#999999").pack(side="right", padx=(4, 0))
    if target_lang == "ru":
        glossary_link = tk.Label(bottom, text=tr("glossary_link"), fg="#7a9cc4",
                                 cursor="hand2", font=("Segoe UI", 8))
        glossary_link.pack(side="right", padx=(4, 4))
        glossary_link.bind("<Button-1>",
                           lambda _e: open_glossary_editor(root, target_lang))
    run_btn = ttk.Button(bottom, text=tr("btn_start"))
    run_btn.pack(side="right", padx=4)
    def clear_all():
        for key in list(history):
            history[key].clear()
        for ui in clients_ui:
            for _ch, (w, _f) in ui["tabs"].items():
                w.config(state="normal")
                w.delete("1.0", "end")
                w.config(state="disabled")
                entry_starts[w] = []

    clear_btn = ttk.Button(bottom, text=tr("btn_clear"), command=clear_all)
    clear_btn.pack(side="right")

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
    search_entry = ttk.Entry(bottom, textvariable=search_var, width=24)
    search_entry.pack(side="left", padx=(12, 2))
    search_entry.bind("<Return>", do_search)
    ttk.Button(bottom, text=tr("btn_search"), command=do_search).pack(side="left")

    # ---------------- логика ----------------
    def collect_cfg():
        cfg.update({
            "clients": list(cfg.get("clients") or []),
            "translate": tr_var.get(),
            "sound": snd_var.get(),
            "notify": ntf_var.get(),
            "show_original": orig_var.get(),
            "on_top": top_var.get(),
            "translate_lang_channels": bool(cfg.get("translate_lang_channels")),
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

    def rebuild_channel(client_idx, channel):
        if client_idx >= len(clients_ui):
            return
        txt, _ = clients_ui[client_idx]["tabs"][channel]
        txt.config(state="normal")
        txt.delete("1.0", "end")
        entry_starts[txt] = []
        for entry in history.get((client_idx, channel), []):
            render_entry(txt, entry)
        txt.see("end")
        txt.config(state="disabled")

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
        if len(history[key]) > HISTORY_MAX:
            history[key] = history[key][-HISTORY_MAX:]
            rebuild_channel(client_idx, channel)   # удалили хвост — перерисовали
        elif client_idx < len(clients_ui):
            txt, _ = clients_ui[client_idx]["tabs"][channel]
            txt.config(state="normal")
            render_entry(txt, entry)
            txt.see("end")
            txt.config(state="disabled")
        # неактивная вкладка с новым сообщением — ярко-синий заголовок
        if client_idx >= len(clients_ui):
            return
        ui = clients_ui[client_idx]
        if (client_idx != current_client["idx"]
                or channel != active_channel(client_idx)):
            ui["tab_buttons"][channel].config(fg=TAB_FG_UNREAD)
        if ui["btn"] is not None and client_idx != current_client["idx"]:
            ui["btn"].config(fg=TAB_FG_UNREAD)

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
                if item[0] != "done":
                    continue
                _, client_idx, channel, t, name, body, translated, src_lang = item
                append_to(client_idx, channel, t, name, body, translated, src_lang)
                state["count"] = state.get("count", 0) + 1
                status_var.set(tr("status_running", n=state["count"],
                                  backend=translator_status_text()))
                if snd_var.get():
                    play_sound(cfg.get("sound_file") or None)
                if ntf_var.get():
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
        def work():
            latest = check_latest_release()
            if latest is None:
                return
            mine = parse_version(APP_VERSION)
            if mine is None or latest <= mine:
                return
            root.after(0, lambda: offer_update(latest))
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

        lang_var = tk.BooleanVar(value=bool(cfg.get("translate_lang_channels")))
        cur_var = tk.BooleanVar(value=bool(cfg.get("translate_currency", True)))
        copy_var = tk.BooleanVar(value=bool(cfg.get("auto_copy_reply", True)))

        def apply(*_args):
            cfg["translate_lang_channels"] = bool(lang_var.get())
            cfg["translate_currency"] = bool(cur_var.get())
            cfg["auto_copy_reply"] = bool(copy_var.get())
            save_config(cfg)

        ttk.Checkbutton(box, text=tr("opt_lang_channels"),
                        variable=lang_var, command=apply).pack(anchor="w",
                                                               pady=2)
        ttk.Checkbutton(box, text=tr("opt_currency"),
                        variable=cur_var, command=apply).pack(anchor="w",
                                                              pady=2)
        ttk.Checkbutton(box, text=tr("opt_auto_copy"),
                        variable=copy_var, command=apply).pack(anchor="w",
                                                               pady=2)
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
        win.geometry("640x340")
        win.transient(root)
        win.minsize(480, 260)

        tree = ttk.Treeview(win, columns=("name", "path"), show="headings",
                            height=10, selectmode="extended")
        tree.heading("name", text=tr("col_name"))
        tree.heading("path", text=tr("col_path"))
        tree.column("name", width=150, anchor="w")
        tree.column("path", width=440, anchor="w")
        tree.pack(fill="both", expand=True, padx=8, pady=8)

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
            d = filedialog.askdirectory(parent=win, title=tr("add_dir_title"))
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

    def restart_gui():
        """Полный перезапуск окна: смена языка интерфейса.

        Потоки слежения останавливаются, окно пересоздаётся с новым языком;
        настройки к этому моменту уже сохранены в конфиг."""
        log_debug("перезапуск GUI (смена языка)")
        stop_event.set()
        try:
            if tray_icon[0] is not None:
                tray_icon[0].stop()
        except Exception:
            pass
        root.destroy()
        run_gui()

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
    try:
        _tr, lang = translate_auto("good evening friends")
        good = lang == "en" and _tr and "вечер" in _tr.lower()
        ok &= good
        backend = "mymemory" if GTX_LAST_ERROR == "429" else "gtx"
        print(f"  [{'OK ' if good else 'FAIL'}] translate ({backend}): {_tr!r} ({lang})")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] translate: {exc}")
    try:
        # явный исходный язык (панель ответа: ru → en)
        _tr2, lang2 = translate_auto("продам кедр и вемп за фавор", dst="en", src="ru")
        good = bool(_tr2) and lang2 in ("ru", "auto")
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
    except Exception as exc:
        ok = False
        print(f"  [FAIL] trade-префиксы: {exc}")
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
        got = currency_restore(_live, amounts, "ru") if _live else None
        good = bool(got) and "20 серебра" in got
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
        run_gui()
    except Exception:
        # Фатальная ошибка mainloop — фиксируем, чтобы было что показать.
        log_debug("ФАТАЛЬНАЯ ОШИБКА GUI:\n" + traceback.format_exc())
        raise
    finally:
        log_debug("=== выход ===")


if __name__ == "__main__":
    main()
