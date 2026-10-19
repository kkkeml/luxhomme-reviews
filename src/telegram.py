"""Отправка готовой HTML-сводки через Telegram Bot API."""

import requests


def send_summary(token, chat_id, summary):
    """Отправить одно сообщение и проверить ответ Telegram."""
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": summary, "parse_mode": "HTML"},
            timeout=30,
        )
    except requests.RequestException:
        # Не показываем текст исключения: в URL запроса находится токен бота.
        raise RuntimeError("Сетевая ошибка при отправке в Telegram") from None
    try:
        answer = response.json()
    except ValueError:
        raise RuntimeError(f"Telegram вернул не JSON (HTTP {response.status_code})") from None
    if response.status_code != 200 or not answer.get("ok"):
        reason = answer.get("description", "неизвестная ошибка")
        raise RuntimeError(f"Telegram: HTTP {response.status_code}: {reason}")
