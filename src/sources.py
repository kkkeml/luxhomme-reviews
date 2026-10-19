"""Чтение отзывов из CSV; позже здесь можно добавить API маркетплейса."""

import csv
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CSV = PROJECT_DIR / "data" / "reviews.csv"
CSV_COLUMNS = ["DATE", "SKU", "RATING", "TEXT"]


def load_csv(path=DEFAULT_CSV):
    """Прочитать CSV и привести названия полей к формату программы."""
    reviews = []
    with Path(path).open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != CSV_COLUMNS:
            raise ValueError(f"Ожидаются колонки: {', '.join(CSV_COLUMNS)}")

        for line_number, row in enumerate(reader, start=2):
            try:
                date = datetime.strptime(row["DATE"], "%d.%m.%Y").strftime("%d.%m.%Y")
                rating = int(row["RATING"])
                sku = row["SKU"].strip()
                text = row["TEXT"].strip()
                if rating not in range(1, 6) or not sku or not text:
                    raise ValueError("пустой SKU/текст или оценка вне диапазона 1–5")
            except (TypeError, ValueError) as error:
                raise ValueError(f"Ошибка в строке {line_number}: {error}") from error

            reviews.append({"date": date, "sku": sku, "rating": rating, "text": text})
    return reviews


def load_reviews(source="csv", csv_path=DEFAULT_CSV):
    """Выбрать источник. Для API позже добавится ещё одна ветка."""
    if source == "csv":
        return load_csv(csv_path)
    raise ValueError(f"Источник {source!r} пока не поддерживается")
