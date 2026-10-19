"""Недельная сводка из отзывов и меток SQLite для Telegram."""

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from html import escape


TOPIC_NAMES = {
    "reliability": "надёжность",
    "performance": "качество работы",
    "delivery": "доставка",
    "noise": "шум",
    "usability": "удобство",
    "value": "цена и польза",
    "packaging": "упаковка",
    "support": "поддержка",
    "design": "внешний вид",
    "other": "другое",
}

SKU_NAMES = {
    "AF5501": "аэрогриль",
    "SM3301": "паровая швабра",
    "WR4742": "робот-мойщик окон",
    "CM1752": "капсульная кофемашина",
}

CHANGE_THRESHOLD = 2


def _short_piece(text, limit):
    """Взять дословный отрывок, не разрывая слово, если это возможно."""
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit + 1)
    if cut < limit // 2:
        cut = limit
    return text[:cut].rstrip()


def _quote(review, label, limit):
    """Брать цитату из метки только тогда, когда она есть в исходном отзыве."""
    text = review["text"]
    quote = label["quote"].strip()
    if not quote or quote not in text:
        sentence_end = re.search(r"[.!?…]+(?=\s|$)", text)
        quote = text[:sentence_end.end()] if sentence_end else text
    return _short_piece(quote, limit)


def _topic_counts(reviews, labels_by_id):
    """Считать темы и жалобы с одним исключением для поломки и результата."""
    counts = {topic: [0, 0] for topic in TOPIC_NAMES}
    complaints = defaultdict(list)
    for review in reviews:
        labels = labels_by_id[review["id"]]
        has_reliability_complaint = any(
            label["topic"] == "reliability" and label["sentiment"] == "neg"
            for label in labels
        )
        for label in labels:
            counts[label["topic"]][0] += 1
            if label["sentiment"] == "neg" and not (
                has_reliability_complaint and label["topic"] == "performance"
            ):
                counts[label["topic"]][1] += 1
                complaints[label["topic"]].append((review, label))
    return counts, complaints


def _complaint_word(number):
    """Подобрать форму слова «жалоба» для числа."""
    if number % 10 == 1 and number % 100 != 11:
        return "жалоба"
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return "жалобы"
    return "жалоб"


def _rating_mismatch(review, labels):
    """Явное расхождение: высокая оценка с жалобой или низкая с похвалой."""
    tones = {label["sentiment"] for label in labels}
    if review["rating"] >= 4:
        return "neg" in tones and "pos" not in tones
    if review["rating"] <= 2:
        return bool(tones) and "neg" not in tones
    return False


def build_weekly_summary(connection):
    """Сформировать HTML; даты отсчитываются от максимальной даты в базе."""
    reviews = [dict(row) for row in connection.execute(
        "SELECT id, date, sku, rating, text FROM reviews"
    )]
    if not reviews:
        raise ValueError("В базе нет отзывов для сводки")
    for review in reviews:
        review["day"] = datetime.strptime(review["date"], "%d.%m.%Y").date()
    last_day = max(review["day"] for review in reviews)
    first_day = last_day - timedelta(days=6)
    previous_first = last_day - timedelta(days=13)
    previous_last = first_day - timedelta(days=1)

    labels_by_id = defaultdict(list)
    for row in connection.execute("SELECT review_id, topic, sentiment, quote FROM labels"):
        labels_by_id[row["review_id"]].append(dict(row))

    current = sorted(
        (review for review in reviews if first_day <= review["day"] <= last_day),
        key=lambda review: (review["day"], review["sku"], review["id"]),
    )
    previous = [review for review in reviews if previous_first <= review["day"] <= previous_last]
    current_counts, complaints = _topic_counts(current, labels_by_id)
    previous_counts, previous_complaints = _topic_counts(previous, labels_by_id)
    current_complaint_reviews = {
        review["id"] for items in complaints.values() for review, _ in items
    }
    previous_complaint_reviews = {
        review["id"] for items in previous_complaints.values() for review, _ in items
    }
    topic_order = list(TOPIC_NAMES)
    top_topics = sorted(
        complaints,
        key=lambda topic: (
            -len(complaints[topic]),
            sum(review["rating"] for review, _ in complaints[topic]) / len(complaints[topic]),
            topic_order.index(topic),
        ),
    )[:3]
    changes = [
        topic for topic in TOPIC_NAMES
        if abs(current_counts[topic][1] - previous_counts[topic][1]) >= CHANGE_THRESHOLD
    ]
    raw_current_performance = sum(
        any(label["topic"] == "performance" and label["sentiment"] == "neg"
            for label in labels_by_id[review["id"]])
        for review in current
    )
    raw_previous_performance = sum(
        any(label["topic"] == "performance" and label["sentiment"] == "neg"
            for label in labels_by_id[review["id"]])
        for review in previous
    )
    filtered_change = current_counts["performance"][1] - previous_counts["performance"][1]
    raw_change = raw_current_performance - raw_previous_performance
    merged_performance_change = "performance" in changes and (
        abs(raw_change) < CHANGE_THRESHOLD or (filtered_change > 0) != (raw_change > 0)
    )
    visible_changes = [
        topic for topic in changes
        if topic != "performance" or not merged_performance_change
    ]
    complaints_by_sku = defaultdict(list)
    for topic, items in complaints.items():
        for review, _ in items:
            complaints_by_sku[review["sku"]].append(topic)
    sku_order = [sku for sku in SKU_NAMES if any(review["sku"] == sku for review in current)]
    sku_order += sorted({review["sku"] for review in current} - set(SKU_NAMES))
    mismatches = [
        review for review in current
        if _rating_mismatch(review, labels_by_id[review["id"]])
    ]

    for quote_limit in (140, 100, 60):
        lines = [
            f"<b>Отзывы за {first_day:%d.%m}–{last_day:%d.%m.%Y}</b> — {len(current)}",
            f"Отзывов с жалобами: было {len(previous_complaint_reviews)} из {len(previous)}, "
            f"стало {len(current_complaint_reviews)} из {len(current)}",
            "",
            "<b>Темы: отзывов / жалоб</b>",
        ]
        empty_topics = []
        for topic, name in TOPIC_NAMES.items():
            total, negative = current_counts[topic]
            if total:
                lines.append(f"• {name}: {total} / {negative}")
            else:
                empty_topics.append(name)
        if empty_topics:
            lines.append(f"без отзывов: {', '.join(empty_topics)}")

        lines += ["", "<b>Топ-3 жалобы</b>"]
        if not top_topics:
            lines.append("Жалоб нет.")
        for number, topic in enumerate(top_topics, 1):
            lines.append(f"{number}. {TOPIC_NAMES[topic]} — {len(complaints[topic])}")
            for review, label in complaints[topic][:2]:
                quote = escape(_quote(review, label, quote_limit), quote=False)
                lines.append(f"   «{quote}» ({escape(review['sku'])}, {review['rating']}★)")

        lines += ["", "<b>По сравнению с прошлой неделей</b>"]
        if visible_changes:
            for topic in visible_changes:
                lines.append(
                    f"• {TOPIC_NAMES[topic]}: было {previous_counts[topic][1]}, "
                    f"стало {current_counts[topic][1]}"
                )
        else:
            lines.append(f"Тем с изменением жалоб на {CHANGE_THRESHOLD} и более нет.")
        if merged_performance_change:
            lines.append("<i>Жалобы на плохой результат у сломанных устройств учтены как надёжность</i>")

        lines += ["", "<b>По товарам</b>"]
        for sku in sku_order:
            topics = complaints_by_sku[sku]
            name = SKU_NAMES.get(sku)
            product = f"{sku} ({name})" if name else sku
            if topics:
                frequency = Counter(topics)
                most_common = max(frequency.values())
                if most_common >= 2:
                    main_topic = next(topic for topic in topic_order if frequency[topic] == most_common)
                    detail = f"чаще всего {TOPIC_NAMES[main_topic]} ({most_common} из {len(topics)})"
                else:
                    names = ", ".join(TOPIC_NAMES[topic] for topic in topic_order if frequency[topic])
                    detail = f"жалобы разные: {names}"
            else:
                detail = "жалоб нет"
            lines.append(
                f"• {escape(product)}: {len(topics)} {_complaint_word(len(topics))}, {detail}"
            )

        lines += ["", f"<b>Оценка и текст расходятся:</b> {len(mismatches)}"]
        for review in mismatches[:2]:
            example = escape(_short_piece(review["text"], 160), quote=False)
            lines.append(f"• «{example}» ({escape(review['sku'])}, {review['rating']}★)")
        lines += ["", "<i>Выборка маленькая: выводы предварительные.</i>"]
        result = "\n".join(lines)
        if len(result) <= 4000:
            return result
    raise ValueError("Сводка длиннее 4000 символов")
