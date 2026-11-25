import argparse
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

import requests


URL = "https://data.austintexas.gov/resource/9t4d-g238.json"
FOLDER = Path(__file__).resolve().parent.parent / "data" / "raw"
PAGE_SIZE = 1000
SELECT_FIELDS = ",".join((
    ":id",
    "age_upon_outcome",
    "animal_id",
    "animal_type",
    "breed",
    "color",
    "date_of_birth",
    "datetime",
    "monthyear",
    "name",
    "outcome_subtype",
    "outcome_type",
    "sex_upon_outcome",
))


def fetch(session, params):
    for attempt in range(3):
        try:
            response = session.get(URL, params=params, timeout=60)
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("Expected a list of rows from Socrata.")
            return rows
        except requests.HTTPError as error:
            status = error.response.status_code
            if status < 500 and status != 429:
                raise RuntimeError(
                    f"Socrata rejected the request (HTTP {status}): "
                    f"{error.response.text.strip()}"
                ) from error
            if attempt == 2:
                raise
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise
        time.sleep(5)


def index_saved_rows(rows):
    if not isinstance(rows, list):
        raise ValueError("Expected a list in outcomes.json. Run with --full to rebuild it.")

    by_id = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("id") or "datetime" not in row:
            raise ValueError(
                "outcomes.json uses a different schema. Back it up if needed, "
                "then run with --full to replace it with archive data."
            )
        record_id = row["id"]
        if record_id in by_id:
            raise ValueError(f"Duplicate saved row ID {record_id!r}. Run with --full.")
        by_id[record_id] = row
    return by_id


def download(session, filters):
    count_rows = fetch(session, {**filters, "$select": "count(*) AS total"})
    expected = int(count_rows[0]["total"])
    downloaded = {}

    for offset in range(0, expected, PAGE_SIZE):
        page = fetch(
            session,
            {
                **filters,
                "$select": SELECT_FIELDS,
                "$order": ":id",
                "$limit": PAGE_SIZE,
                "$offset": offset,
            },
        )
        if len(page) != min(PAGE_SIZE, expected - offset):
            raise RuntimeError(
                f"Unexpected page size at offset {offset}. The source may have changed. "
                "Run the extractor again."
            )

        for row in page:
            record_id = row.pop(":id", None)
            if not record_id:
                raise ValueError("A downloaded row is missing its Socrata :id.")
            if record_id in downloaded:
                raise RuntimeError(f"Socrata returned row {record_id!r} twice at offset {offset}.")
            row["id"] = record_id
            row["outcome_date"] = row.get("datetime")
            row.setdefault("datetime", None)
            downloaded[record_id] = row

        print(f"Unique records fetched: {len(downloaded):,} / {expected:,}", flush=True)

    return downloaded


def write_json(path, records):
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as file:
        temporary = Path(file.name)
        try:
            json.dump(records, file, indent=2, ensure_ascii=False)
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Download Austin's archived animal outcomes.")
    parser.add_argument("--full", action="store_true", help="Replace the saved snapshot.")
    args = parser.parse_args()

    FOLDER.mkdir(parents=True, exist_ok=True)
    output = FOLDER / "outcomes.json"
    saved = {}
    if output.exists() and not args.full:
        with output.open(encoding="utf-8") as file:
            saved = index_saved_rows(json.load(file))

    # The archive exposes ISO-formatted datetime strings. A date-only lower bound
    # includes every time on the first day, regardless of the UTC offset in the text.
    latest = max(
        (date.fromisoformat(row["datetime"][:10]) for row in saved.values() if row["datetime"]),
        default=None,
    )
    filters = {}
    if latest is None:
        print("Full archive download", flush=True)
    else:
        start = latest - timedelta(days=30)
        filters["$where"] = f"datetime >= '{start.isoformat()}'"
        print(f"Incremental pull from {start.isoformat()}", flush=True)

    with requests.Session() as session:
        downloaded = download(session, filters)

    if latest is None and not downloaded:
        raise RuntimeError("The archive returned zero rows. Existing output was preserved.")

    added = len(downloaded.keys() - saved.keys())
    changed = sum(saved[key] != downloaded[key] for key in downloaded.keys() & saved.keys())
    records = list((saved | downloaded).values())
    write_json(output, records)
    write_json(FOLDER / "sample.json", records[:5])

    mode = (
        f"incremental since {start.isoformat()}"
        if latest is not None
        else "full download"
    )

    print(f"\nETL run {datetime.now():%Y-%m-%d %H:%M}", flush=True)
    print(
        f"received from API: {len(downloaded):,} rows ({mode})",
        flush=True,
    )


if __name__ == "__main__":
    main()
