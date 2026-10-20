import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

parser = argparse.ArgumentParser()
parser.add_argument("--full", action="store_true")
args = parser.parse_args()

url = "https://data.austintexas.gov/resource/gsvs-ypi7.json"
folder = Path(__file__).resolve().parent.parent / "data" / "raw"
folder.mkdir(parents=True, exist_ok=True)
output = folder / "outcomes.json"

old_records = []

if output.exists():
    with output.open(encoding="utf-8") as file:
        old_records = json.load(file)

old_by_id = {}

for row in old_records:
    if not row.get("id"):
        raise ValueError("A saved record is missing its ID.")
    if row["id"] in old_by_id:
        raise ValueError("Duplicate ID in saved data.")
    old_by_id[row["id"]] = row

params = {
    "$limit": 1000,
    "$order": "outcome_date, id",
    "$offset": 0
}

if old_records and not args.full:
    latest = max(
        datetime.fromisoformat(row["outcome_date"])
        for row in old_records
        if row.get("outcome_date")
    )
    start = latest - timedelta(days=30)
    start_text = start.isoformat(timespec="milliseconds")
    params["$where"] = f"outcome_date >= '{start_text}'"
    print("Incremental pull from:", start_text)
else:
    print("Full download")

downloaded = []

while True:
    for attempt in range(3):
        try:
            response = requests.get(url, params=params, timeout=60)
            response.raise_for_status()
            batch = response.json()

            if not isinstance(batch, list):
                raise ValueError("Expected a list of records.")

            break

        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise
            time.sleep(5)

    downloaded.extend(batch)
    print("Records fetched:", len(downloaded))

    if len(batch) < params["$limit"]:
        break

    params["$offset"] += len(batch)

new_by_id = {}

for row in downloaded:
    if not row.get("id"):
        raise ValueError("A downloaded record is missing its ID.")
    if row["id"] in new_by_id:
        raise ValueError("Repeated ID during download. Run again.")
    new_by_id[row["id"]] = row

added = sum(key not in old_by_id for key in new_by_id)
changed = sum(
    key in old_by_id and row != old_by_id[key]
    for key, row in new_by_id.items()
)

if args.full:
    merged = new_by_id
else:
    merged = old_by_id.copy()
    merged.update(new_by_id)

records = list(merged.values())

temporary = folder / "outcomes.tmp"

with temporary.open("w", encoding="utf-8") as file:
    json.dump(records, file, indent=2, ensure_ascii=False)

temporary.replace(output)

with (folder / "sample.json").open("w", encoding="utf-8") as file:
    json.dump(records[:5], file, indent=2, ensure_ascii=False)

print("New records:", added)
print("Changed records:", changed)
print("Total saved:", len(records))
print("Finished.")
