#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Наблюдатель за логами Wurm Online.
Играет звук и показывает уведомление при новых событиях в логах.

Отслеживаемые логи (в папке LOG_DIR, с подстановкой текущего года и месяца):
  _Event.YYYY-MM.txt  — реагирует на строку-триггер (--text, по умолчанию
                        "complete the location details")
  _Local.YYYY-MM.txt  — реагирует на ЛЮБОЕ новое событие

По умолчанию новые строки _Local переводятся на русский через публичный
Google gtx endpoint. Исходный язык определяется автоматически (sl=auto),
включая английский и португальский. Перевод идёт в фоновом потоке, не блокирует
слежение; при ошибке строка просто пропускается. Отключить перевод: --notranslate.

При смене месяца скрипт сам переключается на новые файлы.
Ctrl+C — выход.
"""

import argparse
import json
import os
import platform
import queue
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import datetime

DEFAULT_TEXT = "complete the location details"

# Папка с логами и имя персонажа
LOG_DIR = r"e:\SteamLibrary\steamapps\common\Wurm Online\gamedata\players\Uggabuga\logs"

# (префикс файла, триггер: строка для поиска или None = любая непустая строка)
WATCH_FILES = [
    ("_Event", DEFAULT_TEXT),
    ("_Local", None),
]

GTX_URL = "https://translate.googleapis.com/translate_a/single"
GTX_SRC_LANG = "auto"  # Google сам определяет язык: en, pt и др.
GTX_TIMEOUT = 6
TRANSLATE_MAX_CHARS = 1000
TRANSLATE_CACHE_MAX = 5000

PRINT_LOCK = threading.Lock()
TRANSLATION_QUEUE = queue.Queue()
_STOP = threading.Event()


def log(message):
    with PRINT_LOCK:
        print(message)


def strip_log_prefix(line):
    """Убирает ведущий таймстамп Wurm Online: [HH:MM:SS] или [YYYY-MM-DD HH:MM:SS]."""
    return re.sub(r"^\[(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2}\]\s*", "", line).strip()


def google_gtx_translate_ex(text, src=GTX_SRC_LANG, dst="ru", timeout=GTX_TIMEOUT):
    """Перевод через Google gtx. Возвращает (перевод, исходный язык) или (None, None)."""
    if not text:
        return text, src
    if len(text) > TRANSLATE_MAX_CHARS:
        return None, None
    params = urllib.parse.urlencode({
        "client": "gtx",
        "sl": src,
        "tl": dst,
        "dt": "t",
        "q": text,
    })
    url = f"{GTX_URL}?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))

        detected_src = src
        if len(data) > 2 and isinstance(data[2], str) and data[2].strip():
            detected_src = data[2].strip()

        parts = []
        for seg in data[0]:
            if isinstance(seg, list) and seg and isinstance(seg[0], str):
                parts.append(seg[0])
        translated = "".join(parts).strip()
        if not translated:
            return None, None
        return translated, detected_src
    except Exception:
        return None, None


def google_gtx_translate(text, src=GTX_SRC_LANG, dst="ru", timeout=GTX_TIMEOUT):
    """Совместимость: только строка перевода или None."""
    translated, _ = google_gtx_translate_ex(text, src=src, dst=dst, timeout=timeout)
    return translated


NAME_TOKEN_RE = re.compile(r"(<[^<>\r\n]+>)")


def translate_preserving_names(text):
    """Переводит текст, не трогая <ИмяПерсонажа>, и добавляет код языка в конце."""
    def with_lang(translated, detected):
        lang = detected if detected and detected != "auto" else "und"
        return f"{translated} ({lang})"

    parts = NAME_TOKEN_RE.split(text)
    if len(parts) == 1:
        translated, detected = google_gtx_translate_ex(text)
        if translated is None:
            return None
        return with_lang(translated, detected)

    translated_parts = []
    detected_src = None
    need_space_after_name = False
    for part in parts:
        if not part:
            continue
        if part.startswith("<") and part.endswith(">") and len(part) > 2:
            translated_parts.append(part)  # имя персонажа — не переводим
            need_space_after_name = True
            continue
        if not part.strip():
            translated_parts.append(part)  # пробелы/пустоту тоже не шлём в переводчик
            need_space_after_name = False
            continue

        piece, piece_src = google_gtx_translate_ex(part)
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
    if not translated:
        return None
    return with_lang(translated, detected_src)


def translation_worker():
    """Фоновый переводчик строк _Local auto -> RU."""
    cache = {}
    log("[watcher] Переводчик _Local запущен: Google gtx (auto→RU)")
    while not _STOP.is_set():
        try:
            line = TRANSLATION_QUEUE.get(timeout=0.2)
        except queue.Empty:
            continue
        if line is None:
            break

        text = strip_log_prefix(line)
        if not text:
            continue

        if text in cache:
            translated = cache[text]
        else:
            translated = translate_preserving_names(text)
            if translated is None:
                log(f"[translate] ошибка/слишком длинно: {text[:120]}")
                continue
            cache[text] = translated
            if len(cache) > TRANSLATE_CACHE_MAX:
                cache.pop(next(iter(cache)))

        log(f"[local] {text}")
        log(f"[ru]    {translated}")


def current_log_path(prefix):
    """Возвращает путь к логу текущего месяца для данного префикса."""
    now = datetime.datetime.now()
    name = f"{prefix}.{now.year}-{now.month:02d}.txt"
    return os.path.join(LOG_DIR, name)


def play_sound(sound_path=None):
    """Проигрывает звук: файл, если передан, иначе системный сигнал."""
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
            # Системный «бип»
            if system == "Windows":
                import winsound
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            else:
                print("\a", end="", flush=True)
    except Exception as e:
        print(f"[watcher] не удалось проиграть звук: {e}")


def show_notification(title, message):
    """Всплывающее уведомление на рабочем столе (без сторонних библиотек)."""
    import xml.sax.saxutils
    title = xml.sax.saxutils.escape(title)
    message = xml.sax.saxutils.escape(message)
    system = platform.system()
    try:
        if system == "Windows":
            # Нативный тост Windows 10/11 через PowerShell
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
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("WurmWatcher").Show($toast)
"""
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif system == "Darwin":
            subprocess.Popen(["osascript", "-e",
                              f'display notification "{message}" with title "{title}"'])
        else:  # Linux
            try:
                subprocess.Popen(["notify-send", title, message])
            except FileNotFoundError:
                pass
    except Exception as e:
        print(f"[watcher] не удалось показать уведомление: {e}")


class WatchedFile:
    """Состояние слежения за одним логом."""
    def __init__(self, prefix, trigger):
        self.prefix = prefix      # "_Event" / "_Local" и т.п.
        self.trigger = trigger    # строка-триггер или None = любое событие
        self.path = None          # текущий путь (меняется при смене месяца)
        self.position = None      # None = позиция ещё не определена

    def tick(self, sound_path, start_at_end, translate_local=False):
        """Проверяет файл, обрабатывает новые строки. Возвращает число срабатываний."""
        target = current_log_path(self.prefix)
        if target != self.path:
            self.path = target
            self.position = None
            print(f"[watcher] Слежу за файлом: {self.path}")

        if not os.path.exists(self.path):
            return 0

        hits = 0
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            if self.position is None:
                f.seek(0, os.SEEK_END if start_at_end else os.SEEK_SET)
                self.position = f.tell()
                if start_at_end:
                    print(f"[watcher] {self.prefix}: стартую с конца файла — только новые события.")
            f.seek(self.position)
            new_data = f.read()
            if new_data:
                self.position = f.tell()
                for line in new_data.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if self.trigger is None or self.trigger.lower() in line.lower():
                        print(f">>> [{self.prefix}] {line}")
                        if translate_local and self.prefix == "_Local":
                            TRANSLATION_QUEUE.put(line)
                        play_sound(sound_path)
                        show_notification(f"Wurm Online — {self.prefix}", line[:180])
                        hits += 1
            else:
                # Файл мог быть перезаписан — сбрасываем позицию
                if os.path.getsize(self.path) < self.position:
                    self.position = None
        return hits


def main():
    parser = argparse.ArgumentParser(description="Наблюдатель за логами Wurm Online.")
    parser.add_argument("--text", default=DEFAULT_TEXT,
                        help="Строка-триггер для _Event (для _Local срабатывает на любое событие)")
    parser.add_argument("--sound", help="Путь к .wav/.mp3 файлу для оповещения")
    parser.add_argument("--from-start", action="store_true",
                        help="Реагировать и на старые строки в логах (по умолчанию — только на новые после запуска)")
    parser.add_argument("--notranslate", dest="translate_ru", action="store_false",
                        help="Отключить перевод _Local на русский (по умолчанию включён)")
    parser.set_defaults(translate_ru=True)
    args = parser.parse_args()

    watches = [WatchedFile(prefix, args.text if prefix == "_Event" else trigger)
               for prefix, trigger in WATCH_FILES]

    print(f"[watcher] Триггер _Event: '{args.text}' | _Local: любое событие")
    print(f"[watcher] Перевод _Local auto→RU: {'включён (Google gtx)' if args.translate_ru else 'отключён'}")
    if args.from_start and args.translate_ru:
        print("[watcher] Внимание: --from-start с переводом может отправить в перевод много старых строк.")
    print("[watcher] Ctrl+C для выхода.\n")

    worker = None
    if args.translate_ru:
        worker = threading.Thread(target=translation_worker, daemon=True)
        worker.start()

    try:
        while True:
            for w in watches:
                try:
                    w.tick(args.sound, start_at_end=not args.from_start,
                           translate_local=args.translate_ru)
                except PermissionError:
                    pass  # файл занят игрой — подождём
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        _STOP.set()
        if worker is not None:
            worker.join(timeout=1)
        print("\n[watcher] Остановлен.")


if __name__ == "__main__":
    main()
