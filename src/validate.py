"""Сравнить темы и тон из SQLite с эталонной разметкой gold.csv."""

import argparse
import csv
import re
from pathlib import Path

from .classify import TOPICS
from .database import DEFAULT_DB, connect_database, review_id
from .sources import PROJECT_DIR


DEFAULT_GOLD = PROJECT_DIR / "data" / "gold.csv"
RESULTS_DIR = PROJECT_DIR / "results"


def main():
    parser = argparse.ArgumentParser(description="Сравнение разметки с gold.csv")
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()

    with args.gold.open(encoding="utf-8-sig", newline="") as file:
        gold_rows = list(csv.DictReader(file))

    connection = connect_database(args.db)
    exact = correct = predicted = expected = 0
    tone_correct = tone_total = 0
    neg_correct = neg_predicted = neg_expected = 0
    sources = {"llm": 0, "rules": 0}
    models = set()
    versions = set()
    topic_stats = {topic: {"gold": 0, "guessed": 0, "missed": 0, "extra": 0} for topic in TOPICS}
    differences = []
    try:
        for row in gold_rows:
            review = {"date": row["DATE"], "sku": row["SKU"], "text": row["TEXT"]}
            key = review_id(review)
            true_labels = set(row["LABELS"].split("|"))
            result = connection.execute(
                "SELECT topic, sentiment, source, model, prompt_version FROM labels WHERE review_id = ?", (key,)
            ).fetchall()
            for item in result:
                sources[item["source"]] += 1
                if item["source"] == "llm" and item["model"]:
                    models.add(item["model"])
                versions.add(item["prompt_version"].removeprefix("classify_"))
            found_labels = {f"{item['topic']}:{item['sentiment']}" for item in result}
            true_by_topic = dict(label.split(":", 1) for label in true_labels)
            found_by_topic = dict(label.split(":", 1) for label in found_labels)
            common_topics = true_by_topic.keys() & found_by_topic.keys()
            tone_total += len(common_topics)
            tone_correct += sum(true_by_topic[topic] == found_by_topic[topic] for topic in common_topics)

            true_neg = {topic for topic, sentiment in true_by_topic.items() if sentiment == "neg"}
            found_neg = {topic for topic, sentiment in found_by_topic.items() if sentiment == "neg"}
            neg_correct += len(true_neg & found_neg)
            neg_predicted += len(found_neg)
            neg_expected += len(true_neg)

            for topic in true_by_topic.keys() | found_by_topic.keys():
                counts = topic_stats[topic]
                if topic in true_by_topic:
                    counts["gold"] += 1
                if topic in common_topics:
                    counts["guessed"] += 1
                elif topic in true_by_topic:
                    counts["missed"] += 1
                else:
                    counts["extra"] += 1

            exact += found_labels == true_labels
            correct += len(found_labels & true_labels)
            predicted += len(found_labels)
            expected += len(true_labels)
            if found_labels != true_labels:
                differences.append((row["DATE"], row["SKU"], row["TEXT"], true_labels, found_labels))
    finally:
        connection.close()

    precision = correct / predicted if predicted else 0
    recall = correct / expected if expected else 0
    tone_share = tone_correct / tone_total if tone_total else 0
    neg_precision = neg_correct / neg_predicted if neg_predicted else 0
    neg_recall = neg_correct / neg_expected if neg_expected else 0
    summary = [
        f"Полностью совпали: {exact}/{len(gold_rows)} отзывов",
        f"Точность меток: {precision:.1%}; полнота меток: {recall:.1%}",
        f"Совпавший тон среди правильно угаданных тем: {tone_correct}/{tone_total} ({tone_share:.1%})",
        f"Жалобы neg — точность: {neg_precision:.1%}; полнота: {neg_recall:.1%}",
        f"Источник меток: LLM — {sources['llm']}; правила — {sources['rules']}",
    ]
    if sources["rules"]:
        summary.append("ВНИМАНИЕ: среди проверяемых меток есть разметка правилами; это не чистая оценка LLM.")

    table = [
        "Темы (без учёта тона):",
        f"{'Тема':<13} {'Эталон':>7} {'Угадано':>8} {'Пропущено':>10} {'Лишних':>7}",
    ]
    for topic in sorted(topic_stats):
        counts = topic_stats[topic]
        table.append(
            f"{topic:<13} {counts['gold']:>7} {counts['guessed']:>8} "
            f"{counts['missed']:>10} {counts['extra']:>7}"
        )

    def difference_lines(number, item):
        date, sku, text, true_labels, found_labels = item
        return [
            f"  {number}. {date} {sku}: «{text}»",
            f"     эталон: {', '.join(sorted(true_labels))}",
            f"     модель/правила: {', '.join(sorted(found_labels)) or 'нет меток'}",
        ]

    details = [f"Расхождения: {len(differences)}"]
    for number, item in enumerate(differences, start=1):
        details.extend(difference_lines(number, item))

    model_name = next(iter(models)) if len(models) == 1 else "mixed_models" if models else "rules"
    version_name = next(iter(versions)) if len(versions) == 1 else "mixed_versions" if versions else "unknown_version"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", model_name).strip("._-")
    safe_version = re.sub(r"[^A-Za-z0-9._-]+", "_", version_name).strip("._-")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RESULTS_DIR / f"validate_{safe_name}_{safe_version}.txt"
    output_path.write_text("\n".join(summary + [""] + table + [""] + details) + "\n", encoding="utf-8")
    console_lines = summary + [""] + table + ["", details[0]]
    for number, item in enumerate(differences[:5], start=1):
        console_lines.extend(difference_lines(number, item))
    if len(differences) > 5:
        console_lines.append(f"  Остальные {len(differences) - 5} расхождений — в файле.")
    for line in console_lines:
        print(line)
    print(f"Отчёт сохранён: {output_path}")


if __name__ == "__main__":
    main()
