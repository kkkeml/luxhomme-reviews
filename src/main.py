"""Загрузить отзывы из CSV и разметить их в SQLite."""

import argparse
import os
import re
from pathlib import Path

from dotenv import load_dotenv

from .classify import classify_batch
from .database import DEFAULT_DB, connect_database, import_reviews, reviews_to_classify, save_labels, table_count
from .report import build_weekly_summary
from .sources import DEFAULT_CSV, PROJECT_DIR, load_reviews
from .telegram import send_summary


BATCH_SIZE = 10


def main():
    parser = argparse.ArgumentParser(description="Загрузка и разметка отзывов Luxhommè")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="путь к CSV")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="путь к SQLite")
    parser.add_argument("--rules", action="store_true", help="использовать только запасные правила")
    parser.add_argument("--force", action="store_true", help="переразметить уже обработанные отзывы")
    parser.add_argument("--dry-run", action="store_true", help="показать сводку без отправки в Telegram")
    args = parser.parse_args()

    load_dotenv(PROJECT_DIR / ".env")
    api_key = None if args.rules else os.getenv("OPENROUTER_API_KEY")
    model = None if args.rules else os.getenv("OPENROUTER_MODEL")
    prompt_version = os.getenv("PROMPT_VERSION", "v1").strip() or "v1"
    if not re.fullmatch(r"v\d+", prompt_version):
        raise ValueError("PROMPT_VERSION должен иметь вид v1 или v2")
    prompt_path = PROJECT_DIR / "prompts" / f"classify_{prompt_version}.txt"
    prompt = prompt_path.read_text(encoding="utf-8")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not args.dry_run and not (token and chat_id):
        parser.error("для отправки нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")

    reviews = load_reviews(csv_path=args.csv)
    connection = connect_database(args.db)
    try:
        added = import_reviews(connection, reviews)
        if not args.dry_run:
            print(f"CSV: {len(reviews)} отзывов; новых в базе: {added}")

        pending = reviews_to_classify(connection, force=args.force)
        if not args.dry_run:
            print(f"Для разметки: {len(pending)}")
        if pending and not args.rules and not (api_key and model):
            print("Ключ или модель не заданы: использую запасные правила")

        totals = {"first": 0, "retry": 0, "rules": 0}
        for start in range(0, len(pending), BATCH_SIZE):
            batch = pending[start:start + BATCH_SIZE]
            result, phases = classify_batch(batch, prompt, api_key, model, start // BATCH_SIZE + 1)
            for review in batch:
                phase = phases[review["id"]]
                source = "rules" if phase == "rules" else "llm"
                save_labels(
                    connection, review["id"], result[review["id"]],
                    source, model if source == "llm" else None, prompt_version,
                )
                totals[phase] += 1
            if not args.dry_run:
                print(
                    f"Пачка {start // BATCH_SIZE + 1}: {len(batch)} отзывов — "
                    f"с первой попытки {sum(phases[row['id']] == 'first' for row in batch)}, "
                    f"после повтора {sum(phases[row['id']] == 'retry' for row in batch)}, "
                    f"правилами {sum(phases[row['id']] == 'rules' for row in batch)}"
                )

        if not args.dry_run:
            print(
                f"Итого за запуск: с первой попытки {totals['first']}, "
                f"после повторного запроса {totals['retry']}, правилами {totals['rules']}"
            )

            print(f"В базе: {table_count(connection, 'reviews')} отзывов, {table_count(connection, 'labels')} меток")
            examples = connection.execute(
                "SELECT r.date, r.sku, l.topic, l.sentiment, l.quote "
                "FROM labels l JOIN reviews r ON r.id = l.review_id "
                "ORDER BY substr(r.date, 7, 4), substr(r.date, 4, 2), substr(r.date, 1, 2), r.sku LIMIT 5"
            ).fetchall()
            for row in examples:
                print(f"  {row['date']} {row['sku']}: {row['topic']}:{row['sentiment']} — «{row['quote']}»")
        summary = build_weekly_summary(connection)
    finally:
        connection.close()

    if args.dry_run:
        print(summary)
    else:
        send_summary(token, chat_id, summary)
        print("Сводка отправлена в Telegram")


if __name__ == "__main__":
    main()
