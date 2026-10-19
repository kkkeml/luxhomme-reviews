"""Простой Telegram-бот: читает готовые данные SQLite и отвечает на команды."""

import json
import os
import sqlite3
import time
from contextlib import closing

import requests
from dotenv import load_dotenv

from .database import DEFAULT_DB, PROJECT_DIR, connect_database, table_count
from .report import build_weekly_summary


OFFSET_FILE = PROJECT_DIR / "data" / "bot_offset.txt"
API_URL = "https://api.telegram.org/bot"


class TelegramError(Exception):
    """Ошибка Telegram без URL запроса, в котором находится токен."""


def _api_call(session, token, method, *, params=None, data=None, timeout=35):
    """Вызвать Bot API и не выводить токен при сетевой ошибке."""
    try:
        if data is None:
            response = session.get(f"{API_URL}{token}/{method}", params=params, timeout=timeout)
        else:
            response = session.post(f"{API_URL}{token}/{method}", data=data, timeout=timeout)
    except requests.RequestException:
        raise TelegramError("сетевая ошибка Telegram") from None

    try:
        answer = response.json()
    except ValueError:
        raise TelegramError(f"Telegram вернул не JSON (HTTP {response.status_code})") from None
    if response.status_code != 200 or not isinstance(answer, dict) or not answer.get("ok"):
        raise TelegramError(f"Telegram вернул HTTP {response.status_code}")
    return answer.get("result")


def _get_updates(session, token, offset):
    updates = _api_call(
        session, token, "getUpdates",
        params={"offset": offset, "timeout": 25, "allowed_updates": json.dumps(["message"])},
    )
    if not isinstance(updates, list):
        raise TelegramError("неверный формат обновлений Telegram")
    return updates


def _send_message(session, token, chat_id, message, html=False):
    data = {"chat_id": chat_id, "text": message}
    if html:
        data["parse_mode"] = "HTML"
    _api_call(session, token, "sendMessage", data=data)


def _load_offset():
    """После обычного перезапуска продолжить с последнего обновления."""
    if not OFFSET_FILE.exists():
        return 0
    try:
        return int(OFFSET_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        raise ValueError(f"Неверное число в {OFFSET_FILE}") from None


def _save_offset(offset):
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = OFFSET_FILE.with_suffix(".tmp")
    temporary.write_text(str(offset), encoding="utf-8")
    temporary.replace(OFFSET_FILE)


def _summary():
    if not DEFAULT_DB.exists():
        return "Сводка пока недоступна: базы отзывов нет."
    try:
        with closing(connect_database()) as connection:
            return build_weekly_summary(connection)
    except (ValueError, sqlite3.Error):
        return "Сводка пока недоступна: проверьте данные SQLite."


def _status():
    if not DEFAULT_DB.exists():
        return "В базе пока нет отзывов. Разметки нет."
    try:
        with closing(connect_database()) as connection:
            count = table_count(connection, "reviews")
            rows = connection.execute(
                "SELECT source, model, prompt_version, "
                "COUNT(DISTINCT review_id) AS reviews, COUNT(*) AS labels "
                "FROM labels GROUP BY source, model, prompt_version "
                "ORDER BY source, model, prompt_version"
            ).fetchall()
    except sqlite3.Error:
        return "Статус пока недоступен: проверьте данные SQLite."
    lines = [f"В базе: {count} отзывов."]
    if not rows:
        lines.append("Разметки пока нет.")
    else:
        lines.append("Разметка:")
        for row in rows:
            model = row["model"] if row["source"] == "llm" else "правила"
            lines.append(
                f"• {model or 'неизвестная модель'}, {row['prompt_version']}: "
                f"{row['reviews']} отзывов, {row['labels']} меток"
            )
    return "\n".join(lines)


def _answer(text):
    command = ""
    if text and text.strip():
        command = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0]
    if command == "/start":
        return "Привет! Показываю готовую сводку отзывов из SQLite.\n/summary — сводка, /status — состояние базы.\n/help — список команд.", False
    if command == "/help":
        return "/start — что умеет бот\n/help — список команд\n/summary — последняя сводка\n/status — отзывы и версия разметки", False
    if command == "/summary":
        return _summary(), True
    if command == "/status":
        return _status(), False
    return "Не знаю такую команду. Напишите /help.", False


def _process_update(session, token, allowed_chats, update):
    message = update.get("message")
    if not isinstance(message, dict):
        return
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    if str(chat_id) not in allowed_chats:
        _send_message(session, token, chat_id, "Нет доступа")
        return
    answer, html = _answer(message.get("text"))
    _send_message(session, token, chat_id, answer, html=html)


def main():
    load_dotenv(PROJECT_DIR / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    allowed = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS") or os.getenv("TELEGRAM_CHAT_ID", "")
    allowed_chats = {item.strip() for item in allowed.split(",") if item.strip()}
    if not token or not allowed_chats:
        raise SystemExit("Укажите TELEGRAM_BOT_TOKEN и TELEGRAM_ALLOWED_CHAT_IDS (или TELEGRAM_CHAT_ID) в .env")

    offset = _load_offset()
    session = requests.Session()
    print("Бот запущен. Для остановки нажмите Ctrl+C.")
    try:
        while True:
            try:
                for update in _get_updates(session, token, offset):
                    update_id = update.get("update_id")
                    if not isinstance(update_id, int) or update_id < offset:
                        continue
                    _process_update(session, token, allowed_chats, update)
                    offset = update_id + 1
                    _save_offset(offset)
            except TelegramError as error:
                print(f"{error}. Повтор через 5 секунд.")
                time.sleep(5)
    except KeyboardInterrupt:
        print("Бот остановлен.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
