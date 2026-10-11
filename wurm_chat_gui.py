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
APP_VERSION = "1.2.0"
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

# Корень установки Wurm Online (Steam). Подпапка игрока подставляется автоматически.
WURM_PLAYERS_ROOTS = [
    r"e:\SteamLibrary\steamapps\common\Wurm Online\gamedata\players",
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 r"Steam\steamapps\common\Wurm Online\gamedata\players"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                 r"Steam\steamapps\common\Wurm Online\gamedata\players"),
    r"D:\SteamLibrary\steamapps\common\Wurm Online\gamedata\players",
    r"C:\SteamLibrary\steamapps\common\Wurm Online\gamedata\players",
]
# Путь по умолчанию, если автоподстановка не сработала (как в wurm_log_watcher_ru.py)
DEFAULT_LOG_DIR = r"e:\SteamLibrary\steamapps\common\Wurm Online\gamedata\players\Uggabuga\logs"


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
    """Путь к логам: ...\\players\\<текущий игрок Steam>\\logs.

    1) подпапка, совпадающая с PersonaName Steam (без учёта регистра);
    2) иначе персонаж с самой свежей активностью в логах (кто реально играл
       последним — по дате последнего записанного файла в его logs);
    3) иначе единственная подпапка; в крайнем случае — DEFAULT_LOG_DIR.
    """
    players_root = next((r for r in WURM_PLAYERS_ROOTS if os.path.isdir(r)), None)
    if not players_root:
        return DEFAULT_LOG_DIR
    subs = [os.path.join(players_root, d)
            for d in os.listdir(players_root)
            if os.path.isdir(os.path.join(players_root, d))]
    if not subs:
        return DEFAULT_LOG_DIR

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
                    log_debug(f"auto_logs_dir: игрок по PersonaName Steam '{persona}'")
                    return logs

    def last_activity(logs):
        try:
            return max(os.path.getmtime(os.path.join(logs, f))
                       for f in os.listdir(logs))
        except (ValueError, OSError):
            return -1

    best = max((logs_of(d) for d in subs), key=lambda l: last_activity(l) if l else -1)
    if best:
        log_debug(f"auto_logs_dir: персонаж по активности логов: '{os.path.basename(os.path.dirname(best))}'")
        return best
    return DEFAULT_LOG_DIR

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
    ("PM", "PM__", True),
]

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
        return "перевод: Google ✓"
    if st == "mymemory":
        return "перевод: запасной сервис"
    if st == "fallback":
        return "перевод: Google в перерыве, запасной сервис"
    return "перевод: нет данных"


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


def google_gtx_translate_ex(text, dst="ru", timeout=GTX_TIMEOUT):
    """Перевод через Google gtx. Возвращает (перевод, исходный язык) или (None, None)."""
    global GTX_LAST_ERROR
    GTX_LAST_ERROR = None
    if not text:
        return text, "auto"
    if len(text) > TRANSLATE_MAX_CHARS:
        return None, None
    params = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": dst,
                                     "dt": "t", "q": text})
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


def mymemory_translate_ex(text, dst="ru", timeout=GTX_TIMEOUT):
    """Запасной перевод через MyMemory (без ключа, язык определяет сам)."""
    if not text or len(text) > 400:
        return None, None
    try:
        url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(
            {"q": text, "langpair": f"Autodetect|{dst}"})
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


def _mymemory_with_state(text, dst="ru", fallback=False):
    """MyMemory + обновление индикатора состояния переводчика."""
    translated, detected = mymemory_translate_ex(text, dst=dst)
    if translated is not None:
        TRANSLATOR_STATE["backend"] = "fallback" if fallback else "mymemory"
    return translated, detected


def translate_auto(text, dst="ru"):
    """Цепочка бэкендов: Google gtx (с выключателем при 429) → MyMemory."""
    global GTX_BANNED_UNTIL, GTX_FAILS
    if time.monotonic() < GTX_BANNED_UNTIL:
        return _mymemory_with_state(text, dst=dst, fallback=True)
    translated, detected = google_gtx_translate_ex(text, dst=dst)
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
        return _mymemory_with_state(text, dst=dst, fallback=True)
    return _mymemory_with_state(text, dst=dst)


def translate_preserving_names(text, dst="ru"):
    """Переводит текст, не трогая <ИмяПерсонажа>, и добавляет код языка в конце."""
    def with_lang(translated, detected):
        lang = detected if detected and detected != "auto" else "und"
        return f"{translated} ({lang})"

    parts = NAME_TOKEN_RE.split(text)
    if len(parts) == 1:
        translated, detected = translate_auto(text, dst=dst)
        return with_lang(translated, detected) if translated else None

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
            return None
        if detected_src is None and piece_src and piece_src != "auto":
            detected_src = piece_src
        if (need_space_after_name and translated_parts and piece
                and not piece[0].isspace()
                and not translated_parts[-1].endswith((" ", "\t"))):
            translated_parts.append(" ")
        translated_parts.append(piece)
        need_space_after_name = False

    translated = "".join(translated_parts).strip()
    return with_lang(translated, detected_src) if translated else None


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

    Элементы: ("line", channel_name, raw_line).
    При первом обнаружении файл позиционируется в конец (только новые
    сообщения), если start_at_end=True.
    """

    def __init__(self, logs_dir, out_queue, stop_event, start_at_end=True):
        super().__init__(daemon=True)
        self.logs_dir = logs_dir
        self.out = out_queue
        self.stop = stop_event
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
                        self.out.put(("line", name, line))


# --------------------------------------------------------------------------- #
# Переводчик (фоновый поток)
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Жаргон торгового чата: префиксы переводим жёстко, остаток — через переводчик
# --------------------------------------------------------------------------- #

TRADE_PREFIXES = {"WTS": "Продам", "WTB": "Куплю", "PC": "Прайсчек",
                  "WTT": "Обменяю"}
# Реальный формат: «(Cad) WTS …» — серверный тег в скобках перед префиксом,
# префикс может быть в любом регистре («wts», «Wtb»).
RE_TRADE_PREFIX = re.compile(r"^(?:\(([A-Za-z]{2,10})\)\s*)?(WTS|WTB|WTT|PC)\b[:\s-]*",
                             re.IGNORECASE)


def translate_trade_body(body, dst="ru"):
    """WTS/WTB/PC в начале — фиксированный перевод, остаток — обычная цепочка."""
    m = RE_TRADE_PREFIX.match(body)
    if not m:
        return translate_preserving_names(body, dst=dst)
    prefix = m.group(2).upper()
    server = m.group(1)          # серверный тег «(Cad)», может отсутствовать
    rest = body[m.end():]
    head = TRADE_PREFIXES[prefix]
    if server:
        head = f"({server}) {head}"
    if not rest.strip():
        return f"{head} (en)"
    if dst == "ru" and re.search(r"[А-Яа-яЁё]", rest):
        tail, lang = rest, "ru"   # остаток уже на русском
    else:
        tail, detected = translate_auto(rest, dst=dst)
        if tail is None:
            return None
        lang = detected if detected and detected != "auto" else "und"
    return f"{head} {tail} ({lang})"


class TranslateWorker(threading.Thread):
    """Берёт ("line", канал, строка), отдаёт ("done", канал, время, имя,
    оригинал, перевод|None). Переводит только тело сообщения, имя — нет."""

    def __init__(self, raw_queue, done_queue, stop_event, enabled=True,
                 target_lang="ru"):
        super().__init__(daemon=True)
        self.raw = raw_queue
        self.done = done_queue
        self.stop = stop_event
        self.enabled = enabled
        self.target_lang = target_lang
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
        _, channel, line = item

        parsed = parse_chat_line(line)
        if parsed:
            t, name, body = parsed
        else:  # нестандартная строка — покажем как есть
            t, name, body = "", "", strip_log_prefix(line)
        if not body:
            return  # пустые строки вида "[17:33:11] "

        translated = None
        if self.enabled and body:
            trade_prefixed = channel == "Trade" and bool(RE_TRADE_PREFIX.match(body))
            if (self.target_lang == "ru" and re.search(r"[А-Яа-яЁё]", body)
                    and not trade_prefixed):
                translated = body  # уже на русском — перевод не нужен
            elif body in self.cache:
                translated = self.cache[body]
            else:
                pause = TRANSLATE_INTERVAL - (time.monotonic() - self._last_req)
                if pause > 0:
                    time.sleep(pause)
                if trade_prefixed:
                    translated = translate_trade_body(body, self.target_lang)
                else:
                    translated = translate_preserving_names(body, self.target_lang)
                self._last_req = time.monotonic()
                if translated is not None:
                    self.cache[body] = translated
                    if len(self.cache) > TRANSLATE_CACHE_MAX:
                        self.cache.pop(next(iter(self.cache)))

        self.done.put(("done", channel, t, name, body, translated))


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #

DEFAULTS = {
    "logs_dir": DEFAULT_LOG_DIR,
    "translate": True,
    "sound": False,
    "notify": False,
    "on_top": False,
    "show_original": False,
    "sound_file": "",
    "target_lang": "ru",
}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update({k: v for k, v in json.load(f).items() if k in cfg})
        except Exception:
            pass
    # Имя игрока в пути подставляем автоматически по текущему пользователю
    # Steam: сохранённый путь сохраняем, только если он существует; иначе —
    # автоподстановка (players\<текущий игрок>\logs).
    saved_dir = (cfg.get("logs_dir") or "").strip()
    if not saved_dir or not os.path.isdir(saved_dir):
        cfg["logs_dir"] = auto_logs_dir()
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


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

    root = tk.Tk()
    root.title(f"{APP_TITLE} v{APP_VERSION}")
    root.geometry("760x560")
    root.minsize(560, 380)

    stop_event = threading.Event()
    state = {"running": False, "watcher": None, "worker": None,
             "raw": queue.Queue(), "done": queue.Queue()}

    # ---------------- верхняя панель ----------------
    top = ttk.Frame(root)
    top.pack(fill="x", padx=8, pady=(8, 2))
    ttk.Label(top, text="Логи:").pack(side="left")
    dir_var = tk.StringVar(value=cfg["logs_dir"])
    ttk.Entry(top, textvariable=dir_var, width=55).pack(side="left", padx=4)
    ttk.Button(top, text="…", width=3,
               command=lambda: dir_var.set(
                   filedialog.askdirectory() or dir_var.get())).pack(side="left")

    # ---------------- панель флагов ----------------
    flags = ttk.Frame(root)
    flags.pack(fill="x", padx=8, pady=2)
    tr_var = tk.BooleanVar(value=cfg["translate"])
    ttk.Checkbutton(flags, text="Перевод на русский", variable=tr_var).pack(side="left")
    snd_var = tk.BooleanVar(value=cfg["sound"])
    ttk.Checkbutton(flags, text="Звук", variable=snd_var).pack(side="left", padx=8)
    ntf_var = tk.BooleanVar(value=cfg["notify"])
    ttk.Checkbutton(flags, text="Уведомления", variable=ntf_var).pack(side="left")
    orig_var = tk.BooleanVar(value=cfg["show_original"])
    ttk.Checkbutton(flags, text="Оригинал", variable=orig_var).pack(side="left", padx=8)
    top_var = tk.BooleanVar(value=cfg["on_top"])
    top_chk = ttk.Checkbutton(flags, text="Поверх окон", variable=top_var)
    top_chk.pack(side="left")
    root.attributes("-topmost", cfg["on_top"])
    top_chk.config(command=lambda: root.attributes("-topmost", top_var.get()))

    # ---------------- вкладки-окна каналов ----------------
    # Своя полоса вкладок (классические tk.Label): у ttk.Notebook нельзя
    # перекрасить отдельную вкладку, а нам нужен ярко-синий цвет «непрочитано».
    TAB_FG = "#404040"          # обычный цвет заголовка вкладки
    TAB_FG_UNREAD = "#0066ff"   # ярко-синий — новые сообщения
    TAB_BG_ACTIVE = "#cfe3f5"   # фон активной вкладки

    tabbar = tk.Frame(root)
    tabbar.pack(fill="x", padx=8)
    content = ttk.Frame(root)
    content.pack(fill="both", expand=True, padx=8, pady=4)
    tabs = {}          # имя канала -> (виджет текста, frame)
    tab_buttons = {}   # имя канала -> tk.Label-вкладка
    current_tab = {"name": CHANNELS[0][0]}
    history = {name: [] for name, _, _ in CHANNELS}  # хранилище для перерисовки

    for name, _, _ in CHANNELS:
        frame = ttk.Frame(content)
        txt = scrolledtext.ScrolledText(frame, wrap="word", state="disabled",
                                        font=("Segoe UI", 10))
        txt.pack(fill="both", expand=True)
        txt.tag_config("time", foreground="#005a9e")
        txt.tag_config("name", font=("Segoe UI", 10, "bold"))
        txt.tag_config("orig", foreground="#777777")
        txt.tag_config("err", foreground="#cc0000")
        # Все фреймы накладываются друг на друга на всю область content
        # (place, НЕ pack — pack разложил бы их полосами), видимый выбирается
        # через lift() в switch_tab().
        frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        tabs[name] = (txt, frame)
        btn = tk.Label(tabbar, text=name, fg=TAB_FG, padx=8, pady=3,
                       cursor="hand2", font=("Segoe UI", 9))
        btn.pack(side="left")
        btn.bind("<Button-1>", lambda _e, n=name: switch_tab(n))
        tab_buttons[name] = btn

    def _paint_tabs():
        for n, b in tab_buttons.items():
            if n == current_tab["name"]:
                b.config(bg=TAB_BG_ACTIVE, relief="sunken")
            else:
                b.config(bg=root.cget("bg"), relief="flat")

    def switch_tab(name):
        current_tab["name"] = name
        _, frame = tabs[name]
        frame.lift()
        # открыли вкладку — признак «непрочитано» снимается
        tab_buttons[name].config(fg=TAB_FG)
        _paint_tabs()

    def active_tab_name():
        return current_tab["name"]

    # ---------------- панель обратного перевода (RU → EN) ----------------
    reply = ttk.Frame(root)
    reply.pack(fill="x", padx=8, pady=(2, 0))
    ttk.Label(reply, text="Ответ RU→EN:").pack(side="left")
    reply_var = tk.StringVar()
    reply_entry = ttk.Entry(reply, textvariable=reply_var, width=40)
    reply_entry.pack(side="left", padx=(4, 8))
    reply_out_var = tk.StringVar(value="")
    reply_out = ttk.Entry(reply, textvariable=reply_out_var, state="readonly",
                          foreground="#005a9e")
    reply_out.pack(side="left", fill="x", expand=True)

    reply_state = {"after": None, "seq": 0}

    def reply_translate_async(text):
        """Перевод в фоне, результат — в GUI-поток."""
        seq = reply_state["seq"]
        try:
            result, _lang = translate_auto(text, dst="en")
        except Exception:
            result = None
        root.after(0, lambda: reply_show_result(text, result, seq))

    def reply_show_result(text, result, seq):
        if seq != reply_state["seq"]:
            return          # устаревший запрос — текст уже изменился
        if result is None:
            reply_out_var.set("(не удалось перевести)")
            return
        reply_out_var.set(result)
        try:
            root.clipboard_clear()
            root.clipboard_append(result)
            status_var.set(f"Скопировано в буфер: {result[:60]}")
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
    status_var = tk.StringVar(value="Остановлено")
    ttk.Label(bottom, textvariable=status_var).pack(side="left")
    # номер версии — тихий уголок справа
    ttk.Label(bottom, text=f"v{APP_VERSION}",
              foreground="#999999").pack(side="right", padx=(4, 0))
    run_btn = ttk.Button(bottom, text="▶ Старт")
    run_btn.pack(side="right", padx=4)
    def clear_all():
        for ch, (w, _f) in tabs.items():
            w.config(state="normal")
            w.delete("1.0", "end")
            w.config(state="disabled")
            history[ch].clear()

    clear_btn = ttk.Button(bottom, text="Очистить всё", command=clear_all)
    clear_btn.pack(side="right")

    # ---------------- поиск по истории ----------------
    def do_search(event=None):
        query = search_var.get().strip().lower()
        if not query:
            return
        results = []
        for ch_name, entries in history.items():
            for (t, name, original, translated) in entries:
                hay = " ".join(x for x in (name, original, translated) if x).lower()
                if query in hay:
                    results.append((ch_name, t, name, original, translated))
        show_search_results(query, results)

    def show_search_results(query, results):
        win = tk.Toplevel(root)
        win.title(f"Поиск: {query} — найдено: {len(results)}")
        win.geometry("640x400")
        txt = scrolledtext.ScrolledText(win, wrap="word", state="disabled",
                                        font=("Segoe UI", 10))
        txt.pack(fill="both", expand=True)
        txt.tag_config("time", foreground="#005a9e")
        txt.tag_config("name", font=("Segoe UI", 10, "bold"))
        txt.tag_config("ch", foreground="#0066ff")
        txt.tag_config("orig", foreground="#777777")
        lines = []   # индекс строки -> канал для двойного клика
        txt.config(state="normal")
        if not results:
            txt.insert("end", "Ничего не найдено.\n")
        for (ch_name, t, name, original, translated) in results:
            txt.insert("end", f"[{ch_name}] ", "ch")
            if t:
                txt.insert("end", f"[{t}] ", "time")
            if name:
                txt.insert("end", f"{name}: ", "name")
            txt.insert("end", (translated or original) + "\n")
            if translated and translated != original and orig_var.get():
                txt.insert("end", original + "\n", "orig")
            txt.insert("end", "\n")
            lines.append(ch_name)
        txt.config(state="disabled")

        def on_double(_e):
            try:
                idx = txt.index(f"@{_e.x},{_e.y}")
                line_no = int(idx.split(".")[0])
            except Exception:
                return
            # строки результата идут парами/тройками, ищем ближайший сверху канал
            ch = None
            for ln in range(line_no, 0, -1):
                start = f"{ln}.0"
                end = f"{ln}.end"
                content = txt.get(start, end)
                if content.startswith("["):
                    m = re.match(r"\[([^\]]+)\]", content)
                    if m and m.group(1) in tabs:
                        ch = m.group(1)
                        break
            if ch:
                switch_tab(ch)

        txt.bind("<Double-Button-1>", on_double)
        ttk.Label(win, text="Двойной клик по результату — перейти ко вкладке",
                  foreground="#666666").pack(anchor="w", padx=6, pady=2)

    search_var = tk.StringVar()
    search_entry = ttk.Entry(bottom, textvariable=search_var, width=24)
    search_entry.pack(side="left", padx=(12, 2))
    search_entry.bind("<Return>", do_search)
    ttk.Button(bottom, text="🔍 Найти", command=do_search).pack(side="left")

    # ---------------- логика ----------------
    def collect_cfg():
        cfg.update({
            "logs_dir": dir_var.get().strip(),
            "translate": tr_var.get(),
            "sound": snd_var.get(),
            "notify": ntf_var.get(),
            "show_original": orig_var.get(),
            "on_top": top_var.get(),
        })
        save_config(cfg)

    HISTORY_MAX = 1000   # сколько сообщений хранить по каждому каналу

    def render_entry(txt, entry):
        """Вставить одно сообщение в виджет с учётом текущих галочек."""
        t, name, original, translated = entry
        if t:
            txt.insert("end", f"[{t}] ", "time")
        if name:
            txt.insert("end", f"{name}: ", "name")
        if translated is None:
            # перевода нет — оригинал показываем в любом случае
            txt.insert("end", original + "\n", "orig")
            if tr_var.get() and original:
                txt.insert("end", "(не удалось перевести)\n", "err")
        elif translated == original:
            txt.insert("end", original + "\n")      # сообщение уже на русском
        elif orig_var.get():
            txt.insert("end", original + "\n", "orig")
            txt.insert("end", translated + "\n")
        else:
            txt.insert("end", translated + "\n")    # только перевод
        txt.insert("end", "\n")

    def rebuild_channel(channel):
        txt, _ = tabs[channel]
        txt.config(state="normal")
        txt.delete("1.0", "end")
        for entry in history[channel]:
            render_entry(txt, entry)
        txt.see("end")
        txt.config(state="disabled")

    def refresh_view():
        """Перерисовать все вкладки (например, после переключения «Оригинал»)."""
        for ch in tabs:
            rebuild_channel(ch)

    # переключение «Оригинал» применяется и к уже выведенной истории
    orig_var.trace_add("write", lambda *_: refresh_view())

    def append_to(channel, time_s, name, original, translated):
        entry = (time_s, name, original, translated)
        history[channel].append(entry)
        if len(history[channel]) > HISTORY_MAX:
            history[channel] = history[channel][-HISTORY_MAX:]
            rebuild_channel(channel)   # удалили хвост — перерисовали
        else:
            txt, _ = tabs[channel]
            txt.config(state="normal")
            render_entry(txt, entry)
            txt.see("end")
            txt.config(state="disabled")
        # неактивная вкладка с новым сообщением — ярко-синий заголовок
        if channel != active_tab_name():
            tab_buttons[channel].config(fg=TAB_FG_UNREAD)

    def start():
        collect_cfg()
        if not cfg["logs_dir"] or not os.path.isdir(cfg["logs_dir"]):
            status_var.set("Укажите существующую папку логов: …\\players\\<Игрок>\\logs")
            return
        stop_event.clear()
        state["raw"] = queue.Queue()
        state["done"] = queue.Queue()
        state["watcher"] = LogWatcher(cfg["logs_dir"], state["raw"], stop_event,
                                      start_at_end=True)
        state["worker"] = TranslateWorker(state["raw"], state["done"], stop_event,
                                          enabled=cfg["translate"],
                                          target_lang=cfg.get("target_lang", "ru"))
        state["watcher"].start()
        state["worker"].start()
        state["running"] = True
        run_btn.config(text="■ Стоп")
        mode = "только новые сообщения (старые пропущены)"
        state["count"] = 0
        status_var.set(f"Слежу за {len(CHANNELS)} каналами, {mode}")
        log_debug(f"старт: logs_dir={cfg['logs_dir']} translate={cfg['translate']}")

    def stop():
        stop_event.set()
        state["running"] = False
        run_btn.config(text="▶ Старт")
        status_var.set("Остановлено")
        log_debug("стоп")

    def toggle():
        stop() if state["running"] else start()
    run_btn.config(command=toggle)

    def supervise():
        """Перезапуск фоновых потоков, если они неожиданно умерли."""
        if not state["running"]:
            return
        if state["watcher"] is not None and not state["watcher"].is_alive():
            log_debug("надзор: наблюдатель умер — перезапуск")
            state["watcher"] = LogWatcher(cfg["logs_dir"], state["raw"], stop_event,
                                          start_at_end=True)
            state["watcher"].start()
            status_var.set("Наблюдатель перезапущен после сбоя (см. debug-лог)")
        if state["worker"] is not None and not state["worker"].is_alive():
            log_debug("надзор: переводчик умер — перезапуск")
            state["worker"] = TranslateWorker(state["raw"], state["done"], stop_event,
                                              enabled=tr_var.get(),
                                              target_lang=cfg.get("target_lang", "ru"))
            state["worker"].start()
            status_var.set("Переводчик перезапущен после сбоя (см. debug-лог)")

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
                status_var.set(f"Работает. Поймано сообщений: {state.get('count', 0)}. "
                               f"{translator_status_text()}")
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
                _, channel, t, name, body, translated = item
                append_to(channel, t, name, body, translated)
                state["count"] = state.get("count", 0) + 1
                status_var.set(f"Работает. Поймано сообщений: {state['count']}. "
                               f"{translator_status_text()}")
                if snd_var.get():
                    play_sound(cfg.get("sound_file") or None)
                if ntf_var.get():
                    show_notification(f"Wurm — {channel}",
                                      (translated or body)[:180])
        except queue.Empty:
            pass
        except Exception as exc:    # цикл опроса не должен умирать молча
            log_debug(f"poll: ошибка вывода: {exc!r}\n" + traceback.format_exc())
            status_var.set(f"Ошибка вывода: {exc!r}")
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
                pystray.MenuItem("Показать", lambda *_: tray_show(), default=True),
                pystray.MenuItem("Выход", lambda *_: tray_quit()),
            )
            icon = pystray.Icon("WurmChatTranslator", make_tray_icon(),
                                APP_TITLE, menu)
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
                APP_TITLE,
                f"Доступна новая версия v{newer} (у вас v{APP_VERSION}).\n\n"
                f"Открыть страницу загрузки?\n"
                f"(Скачанный exe можно положить поверх старого — настройки сохранятся)",
                icon="info"):
            try:
                os.startfile(RELEASES_PAGE)
            except Exception as exc:
                log_debug(f"не удалось открыть страницу релизов: {exc!r}")

    root.protocol("WM_DELETE_WINDOW", on_close)
    switch_tab(CHANNELS[0][0])      # поднять первую вкладку
    root.after(100, poll)
    root.after(300, start)          # слежение запускается сразу при старте
    root.after(5000, check_updates_silent)   # проверка обновлений — в фоне
    root.mainloop()


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

def self_test():
    ok = True
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
        tr, lang = translate_auto("good evening friends")
        good = lang == "en" and tr and "вечер" in tr.lower()
        ok &= good
        backend = "mymemory" if GTX_LAST_ERROR == "429" else "gtx"
        print(f"  [{'OK ' if good else 'FAIL'}] translate ({backend}): {tr!r} ({lang})")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] translate: {exc}")
    try:
        got = translate_trade_body("WTS rare pickaxe, 5s")
        good = got is not None and got.startswith("Продам")
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] trade WTS: {got!r}")
        got = translate_trade_body("wtb: bulk cotton")
        good = got is not None and got.startswith("Куплю")
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] trade WTB: {got!r}")
        got = translate_trade_body("PC on a rare hammer")
        good = got is not None and got.startswith("Прайсчек")
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] trade PC: {got!r}")
        got = translate_trade_body("(Cad) WTS wemp, cotton, barley")
        good = got is not None and got.startswith("(Cad) Продам")
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] trade серверный тег: {got!r}")
        got = translate_trade_body("(Mel) wtb rare leather cap")
        good = got is not None and "(Mel) Куплю" in got
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] trade тег+регистр: {got!r}")
        got = translate_trade_body("(Xan) WTT rare sword for axe")
        good = got is not None and "(Xan) Обменяю" in got
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] trade WTT: {got!r}")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] trade-префиксы: {exc}")
    persona = steam_persona_name()
    auto = auto_logs_dir()
    good = os.path.isdir(auto)
    ok &= good
    print(f"  [{'OK ' if good else 'FAIL'}] steam-пользователь: {persona!r}; "
          f"логи: {auto}")
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
    mutex_handle, already = _single_instance_mutex()
    if already:
        log_debug("обнаружен уже запущенный экземпляр")
        import tkinter as tk
        from tkinter import messagebox
        ask = tk.Tk()
        ask.withdraw()
        ask.attributes("-topmost", True)
        choice = messagebox.askyesnocancel(
            APP_TITLE,
            "Приложение уже запущено.\n\n"
            "Да — активировать уже запущенное\n"
            "Нет — закрыть его и запустить новое\n"
            "Отмена — ничего не делать")
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
