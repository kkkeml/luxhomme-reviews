"""Хранение отзывов и разметки в SQLite."""

import hashlib
import sqlite3
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_DIR / "data" / "reviews.db"


def review_id(review):
    """Одинаковая дата, SKU и текст всегда дают один и тот же ID."""
    value = f"{review['date']}\0{review['sku']}\0{review['text']}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def connect_database(path=DEFAULT_DB):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            sku TEXT NOT NULL,
            rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
            text TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS labels (
            review_id TEXT NOT NULL,
            topic TEXT NOT NULL CHECK (topic IN (
                'reliability', 'performance', 'delivery', 'noise', 'usability',
                'value', 'packaging', 'support', 'design', 'other'
            )),
            sentiment TEXT NOT NULL CHECK (sentiment IN ('pos', 'neg', 'neu')),
            quote TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('llm', 'rules')),
            model TEXT,
            prompt_version TEXT NOT NULL,
            PRIMARY KEY (review_id, topic),
            FOREIGN KEY (review_id) REFERENCES reviews(id) ON DELETE CASCADE
        );
        """
    )
    # В уже созданной базе старые метки получены правилами и промптом v1.
    columns = {row[1] for row in connection.execute("PRAGMA table_info(labels)")}
    if "source" not in columns:
        connection.execute("ALTER TABLE labels ADD COLUMN source TEXT NOT NULL DEFAULT 'rules' CHECK (source IN ('llm', 'rules'))")
    if "model" not in columns:
        connection.execute("ALTER TABLE labels ADD COLUMN model TEXT")
    if "prompt_version" not in columns:
        connection.execute("ALTER TABLE labels ADD COLUMN prompt_version TEXT NOT NULL DEFAULT 'classify_v1'")
    connection.commit()
    return connection


def import_reviews(connection, reviews):
    """Повторный импорт пропускает отзывы с уже известным ID."""
    added = 0
    with connection:
        for review in reviews:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO reviews (id, date, sku, rating, text) VALUES (?, ?, ?, ?, ?)",
                (review_id(review), review["date"], review["sku"], review["rating"], review["text"]),
            )
            added += cursor.rowcount
    return added


def reviews_to_classify(connection, force=False):
    """Вернуть все отзывы или только те, у которых ещё нет разметки."""
    condition = "" if force else "WHERE NOT EXISTS (SELECT 1 FROM labels WHERE review_id = reviews.id)"
    rows = connection.execute(
        f"SELECT id, date, sku, rating, text FROM reviews {condition} "
        "ORDER BY substr(date, 7, 4), substr(date, 4, 2), substr(date, 1, 2), id"
    ).fetchall()
    return [dict(row) for row in rows]


def save_labels(connection, review_id_value, labels, source, model, prompt_version):
    """Заменить старую разметку одного отзыва новой."""
    with connection:
        connection.execute("DELETE FROM labels WHERE review_id = ?", (review_id_value,))
        connection.executemany(
            "INSERT INTO labels (review_id, topic, sentiment, quote, source, model, prompt_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (review_id_value, item["topic"], item["sentiment"], item["quote"], source, model, prompt_version)
                for item in labels
            ],
        )


def table_count(connection, table):
    if table not in {"reviews", "labels"}:
        raise ValueError("Неизвестная таблица")
    return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
