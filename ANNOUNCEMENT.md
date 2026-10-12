# Анонс релиза v1.2.0 — Wurm Chat Translator

Тексты для публикации. Русская версия — основная (аудитория переводчика — русскоязычные игроки). Английская — короткая, для r/WurmOnline.

---

## Русская версия (форум Wurm, русскоязычные сообщества)

**Заголовок:** Wurm Chat Translator v1.2.0 — переводчик игрового чата (Local, Trade, GL-Freedom и др.)

Устали копировать чат в переводчик построчно? Wurm Chat Translator — небольшая программа для Windows, которая следит за логами Wurm Online и показывает сообщения чата в реальном времени, с переводом на русский.

**Как это работает:** запустили exe — и всё. Программа сама находит папку логов по вашему нику Steam, ловит новые сообщения из чатов и переводит их. Никаких ключей API, настроек и копипасты.

**Возможности:**

- **Вкладки по каналам** — Local, Village, Alliance, Trade, Freedom, GL-Freedom, CA Help, личные сообщения. Вкладка с новыми сообщениями подсвечивается ярко-синим, при открытии признак пропадает
- **Перевод на русский** — через Google Translate, при недоступности автоматически переключается на запасной сервис. Сообщения на кириллице не переводятся
- **Торговый чат** — префиксы переводятся автоматически: WTS → «Продам», WTB → «Куплю», WTT → «Обменяю», PC → «Прайсчек»
- **Панель ответа** — вводите текст на русском внизу окна, получаете перевод на английский, который сразу копируется в буфер обмена
- **Оригинал** — одной галочкой можно показывать или скрывать исходный текст сообщений
- **Уведомления и звук** — появились новые сообщения, пока вы не в игре (по умолчанию выключены)
- **Одно окно** — повторный запуск не создаёт копии программы
- **Поиск по истории** и автоматическая проверка обновлений

**Установка:**

1. Скачайте `WurmChatTranslator.exe` со страницы релизов: https://github.com/UggaBuga/wurm-chat-translator/releases/latest
2. Запустите. Всё.

**Требования:** Windows, Wurm Online (Steam-версия).

Исходный код открыт (MIT): https://github.com/UggaBuga/wurm-chat-translator

---

## English version (r/WurmOnline, short)

**Title:** Wurm Chat Translator v1.2.0 — real-time chat translation tool for Russian-speaking players

A small Windows app that watches your Wurm Online chat logs and shows incoming messages per channel, translated into Russian. Made for Russian-speaking players on Freedom/Defiance who want to follow Local, Trade and GL-Freedom without copy-pasting into a translator.

- Tabs per chat channel (Local, Village, Alliance, Trade, Freedom, GL-Freedom, PM) with unread highlighting
- Translation via Google Translate, automatic fallback to a backup service when Google is unavailable; Cyrillic messages are left as-is
- Trade prefixes translated: WTS/WTB/WTT/PC → Russian equivalents
- Quick-reply panel: type in Russian, get English translation copied to clipboard
- Auto-detects your log folder from your Steam profile; works out of the box, no API keys
- Open source (MIT), single .exe, no installation

Download: https://github.com/UggaBuga/wurm-chat-translator/releases/latest
