"""Разметка отзывов через OpenRouter и запасные простые правила."""

import json
import re
import string
import time
from functools import lru_cache
from pathlib import Path

import requests


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL_URL = "https://openrouter.ai/api/v1/model/"
TOPICS = {
    "reliability", "performance", "delivery", "noise", "usability",
    "value", "packaging", "support", "design", "other",
}
SENTIMENTS = {"pos", "neg", "neu"}
LOGS_DIR = Path(__file__).resolve().parents[1] / "logs"

# Здесь короткие фразы намеренно простые: это запасной вариант, а не замена LLM.
RULES = {
    "reliability": {
        "neg": ["перестал", "перестала", "не включ", "не подключ", "не цепляется", "не гре", "пропал", "брак", "подтека", "течет", "то греет то нет", "не держит", "тэн"],
        "pos": ["работает исправно", "работает без проблем"],
    },
    "performance": {
        "neg": ["развод", "сыр", "мокр", "пол прям сыр", "луж", "не отм", "неравномер", "не пропек", "пар вообще нет", "пара нет"],
        "pos": ["готовит быстро", "корочка", "вкусн", "отмыл", "отмыла", "отошел", "помыл", "чист", "сочн", "моет чисто", "пенк", "румян", "пар идет", "пар идёт", "парит", "результат хороший", "хорошо работает", "получил", "запекла"],
    },
    "delivery": {
        "neg": ["доставка 9 дней", "ехал больше недели", "доставку задерж", "долго ехал"],
        "pos": ["доставка быстра", "быстро достав", "пришла быстро"],
    },
    "noise": {
        "neg": ["шум", "гудит гром", "громче"],
        "pos": ["тихо работает", "не шумит"],
    },
    "usability": {
        "neg": ["тяжел", "тяжелов", "не влез", "не помещ", "неудоб", "снимать поддон", "убирать подставку"],
        "pos": ["удоб", "прост", "понятн", "разобралась", "собрала за", "с пульта"],
    },
    "value": {
        "neg": ["за такие деньги", "денег не стоит", "дорого"],
        "pos": ["за свою цену", "цена отлич", "приемлемые деньги"],
    },
    "packaging": {
        "neg": ["вскрыт", "мят", "помят", "поврежд"],
        "pos": ["коробка цел", "хорошо упак", "упакована"],
    },
    "support": {
        "neg": ["продавец не ответ", "поддержка не ответ"],
        "pos": ["продавец помог", "поддержка помог"],
        "neu": ["как связаться", "вопрос продавцу"],
    },
    "design": {
        "neg": ["некрасив"],
        "pos": ["симпатич", "аккуратн", "красив", "внешний вид"],
    },
}


def _fragment(text, phrase):
    """Взять дословный кусок текста рядом с найденной фразой."""
    place = text.casefold().find(phrase)
    marks = ".!?"
    left = max(text.rfind(mark, 0, place) for mark in marks)
    right_positions = [text.find(mark, place + len(phrase)) for mark in marks]
    right_positions = [position for position in right_positions if position >= 0]
    right = min(right_positions) + 1 if right_positions else len(text)
    return text[left + 1:right].strip()


def classify_by_rules(review):
    """Вернуть несколько тем; оценку в звёздах не использовать."""
    text = review["text"]
    lowered = text.casefold()
    labels = []
    for topic, sentiments in RULES.items():
        # Негатив проверяется первым, если по одной теме есть и плюс, и минус.
        for sentiment in ("neg", "pos", "neu"):
            match = next((phrase for phrase in sentiments.get(sentiment, []) if phrase in lowered), None)
            if match:
                labels.append({"topic": topic, "sentiment": sentiment, "quote": _fragment(text, match)})
                break

    if not labels:
        sentiment = "pos" if any(word in lowered for word in ("супер", "отлично", "огонь")) else "neu"
        labels.append({"topic": "other", "sentiment": sentiment, "quote": text[:80]})
    return labels


def _fallback_quote(text):
    """Взять первое короткое предложение или обрезать на границе слова."""
    sentence_end = re.search(r"[.!?…]+(?=\s|$|[А-ЯЁA-Z])", text)
    if sentence_end and sentence_end.end() <= 80:
        return text[:sentence_end.end()].strip()
    if len(text) <= 80:
        return text.strip()
    spaces = list(re.finditer(r"\s+", text[:81]))
    cut = spaces[-1].start() if spaces else 80
    return text[:cut].rstrip() + "…"


def _checked_quote(text, quote):
    """Найти цитату без учёта регистра, пробелов и знаков по краям."""
    if isinstance(quote, str):
        edge_marks = string.punctuation + "«»„“”…—– \t\r\n"
        core = quote.strip(edge_marks)
        if core:
            pattern = r"\s+".join(re.escape(word) for word in core.split())
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return text[match.start():match.end()], False
    return _fallback_quote(text), True


def validate_answer(content, batch, warnings=None):
    """Проверить полученные отзывы, сохраняя корректную часть ответа."""
    answer = json.loads(content)
    if not isinstance(answer, dict) or not isinstance(answer.get("reviews"), list):
        raise ValueError("проверка ответа: нет списка reviews")
    items = answer["reviews"]
    expected = {review["id"]: review["text"] for review in batch}

    checked = {}
    for item in items:
        try:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item.get("id") not in expected:
                raise ValueError(f"неизвестный ID отзыва: {item.get('id') if isinstance(item, dict) else item!r}")
            review_id = item["id"]
            if review_id in checked:
                raise ValueError(f"повтор ID отзыва: {review_id[:8]}")
            if not isinstance(item.get("labels"), list) or not item["labels"]:
                raise ValueError(f"пустой список тем у отзыва {review_id[:8]}")
            text = expected[review_id]
            topics_seen = {}
            for label in item["labels"]:
                if not isinstance(label, dict):
                    raise ValueError(f"неверный формат темы у отзыва {review_id[:8]}")
                topic = label.get("topic")
                sentiment = label.get("sentiment")
                quote = label.get("quote")
                if not isinstance(topic, str) or topic not in TOPICS:
                    raise ValueError(f"тема не из списка: {topic!r} у отзыва {review_id[:8]}")
                if not isinstance(sentiment, str) or sentiment not in SENTIMENTS:
                    raise ValueError(f"тон не из списка: {sentiment!r} у отзыва {review_id[:8]}")
                quote, replaced = _checked_quote(text, quote)
                if replaced and warnings is not None:
                    warnings.append(f"цитата не совпала у отзыва {review_id[:8]}, подставлено начало отзыва")

                new_label = {"topic": topic, "sentiment": sentiment, "quote": quote}
                previous = topics_seen.get(topic)
                if previous is None:
                    topics_seen[topic] = new_label
                elif previous["sentiment"] != sentiment:
                    if warnings is not None:
                        warnings.append(f"конфликт тона: тема {topic}, отзыв {review_id}")
                    if sentiment == "neg":
                        topics_seen[topic] = new_label
                    else:
                        previous["sentiment"] = "neg"
            checked[review_id] = list(topics_seen.values())
        except ValueError as error:
            if warnings is None:
                raise
            warnings.append(str(error))
    return checked


def _save_raw_answer(batch_number, attempts, api_key):
    """Сохранить ответы попыток без ключа API."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGS_DIR / f"batch_{batch_number}_raw.txt"
    text = "\n\n".join(attempts)
    if api_key:
        text = text.replace(api_key, "[СКРЫТО]")
    path.write_text(text + "\n", encoding="utf-8")
    return path


@lru_cache(maxsize=16)
def _supports_seed(model):
    """Проверить по каталогу OpenRouter, принимает ли модель seed."""
    try:
        response = requests.get(MODEL_URL + model, timeout=10)
        response.raise_for_status()
        return "seed" in response.json()["data"]["supported_parameters"]
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return False


def classify_with_llm(batch, prompt, api_key, model, batch_number=1):
    """Повторять запрос только для отзывов без корректной разметки."""
    remaining = {row["id"]: row for row in batch}
    results = {}
    phases = {}
    attempts = []
    seed_supported = _supports_seed(model)
    for attempt in range(4):
        if not remaining:
            break
        requested = list(remaining.values())
        reviews_for_model = [{"id": row["id"], "text": row["text"]} for row in requested]
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps({"reviews": reviews_for_model}, ensure_ascii=False)},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        if seed_supported:
            payload["seed"] = 42
        response = None
        raw_answer = ""
        try:
            response = requests.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=60,
            )
            raw_answer = response.text
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("поле content в ответе API не является строкой")
            raw_answer = content
            warnings = []
            checked = validate_answer(content, requested, warnings)
            for review_id, labels in checked.items():
                results[review_id] = labels
                phases[review_id] = "first" if attempt == 0 else "retry"
                del remaining[review_id]
            if remaining:
                reason = f"не хватает отзывов в ответе: запрошено {len(requested)}, размечено {len(checked)}, осталось {len(remaining)}"
                print(f"Пачка {batch_number}, попытка {attempt + 1}/4: {reason}")
            attempts.append(
                f"Попытка {attempt + 1}, HTTP {response.status_code}, запрошено {len(requested)}, "
                f"размечено {len(checked)}, осталось {len(remaining)}\n{raw_answer}"
                + ("\nЛог: " + "; ".join(warnings) if warnings else "")
            )
            _save_raw_answer(batch_number, attempts, api_key)
            for warning in warnings:
                print(f"Пачка {batch_number}: {warning}")
        except requests.HTTPError as error:
            status = response.status_code if response is not None else "?"
            reason = f"HTTP {status}: {error}"
        except requests.exceptions.JSONDecodeError as error:
            reason = f"ошибка разбора JSON ответа API: {error}"
        except json.JSONDecodeError as error:
            reason = f"ошибка разбора JSON модели: строка {error.lineno}, столбец {error.colno}: {error.msg}"
        except requests.RequestException as error:
            reason = f"сетевая ошибка: {error}"
        except ValueError as error:
            reason = str(error)
        except (KeyError, IndexError, TypeError) as error:
            reason = f"неожиданный формат ответа API: {type(error).__name__}: {error}"
        else:
            reason = None

        if reason is not None:
            status = response.status_code if response is not None else "нет ответа"
            attempts.append(f"Попытка {attempt + 1}, HTTP {status}, ошибка: {reason}\n{raw_answer}")
            _save_raw_answer(batch_number, attempts, api_key)
            print(f"Пачка {batch_number}, попытка {attempt + 1}/4: {reason}")
        if remaining and attempt < 3:
            time.sleep(2 ** attempt)
    return results, phases


def classify_batch(batch, prompt, api_key=None, model=None, batch_number=1):
    """Разметить LLM доступные отзывы, остальные — правилами."""
    if api_key and model:
        results, phases = classify_with_llm(batch, prompt, api_key, model, batch_number)
    else:
        results, phases = {}, {}
    for row in batch:
        if row["id"] not in results:
            results[row["id"]] = classify_by_rules(row)
            phases[row["id"]] = "rules"
    return results, phases
