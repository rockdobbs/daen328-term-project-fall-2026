"""Load cleaned Austin Animal Center outcomes into PostgreSQL.

Schema (3NF):
    animals    one row per animal               PK animal_id
    outcomes   one row per outcome event        PK outcome_id, FK animal_id -> animals
    etl_state  where the last run left off      PK source

Every insert is an upsert on the primary key, so running the pipeline
again never creates duplicate rows. Rows whose values did not change are
skipped, so the summary only counts real inserts and updates.
"""

import os
from datetime import datetime
from pathlib import Path
from pprint import pprint
import pandas as pd
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

SOURCE_NAME = "austin_animal_outcomes"

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS animals (
    animal_id        VARCHAR(20) PRIMARY KEY,
    name             VARCHAR(100),
    animal_type      VARCHAR(50)  NOT NULL,
    sex              VARCHAR(20),
    primary_breed    VARCHAR(100),
    primary_color    VARCHAR(50),
    secondary_color  VARCHAR(50),
    date_of_birth    DATE
);

CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id         TEXT PRIMARY KEY,
    animal_id          VARCHAR(20) NOT NULL REFERENCES animals (animal_id),
    outcome_date       TIMESTAMP   NOT NULL,
    intake_date        TIMESTAMP,
    days_in_shelter    INTEGER,
    outcome_status     VARCHAR(50) NOT NULL,
    euthanasia_reason  VARCHAR(50),
    spayed_neutered    VARCHAR(20),
    source_timestamp   TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_outcomes_animal_id ON outcomes (animal_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_outcome_date ON outcomes (outcome_date);

CREATE TABLE IF NOT EXISTS etl_state (
    source             VARCHAR(50) PRIMARY KEY,
    last_outcome_date  TIMESTAMP,
    last_run_at        TIMESTAMP NOT NULL
);
"""

# DataFrame column (from transform) -> database column
ANIMAL_COLUMNS = {
    "animal_id": "animal_id",
    "name": "name",
    "type": "animal_type",
    "sex": "sex",
    "primary_breed": "primary_breed",
    "primary_color": "primary_color",
    "secondary_color": "secondary_color",
    "date_of_birth": "date_of_birth",
}

OUTCOME_COLUMNS = {
    "id": "outcome_id",
    "animal_id": "animal_id",
    "outcome_date": "outcome_date",
    "intake_date": "intake_date",
    "days_in_shelter": "days_in_shelter",
    "outcome_status": "outcome_status",
    "euthanasia_reason": "euthanasia_reason",
    "spayed_neutered": "spayed_neutered",
    "timestamp": "source_timestamp",
}


def get_connection():
    """Open a PostgreSQL connection using credentials from .env."""
    load_dotenv()
    return psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ["POSTGRES_PORT"],
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )


def create_tables(conn) -> None:
    """Create the tables if they don't exist yet. Safe to run every time."""
    with conn.cursor() as cur:
        cur.execute(CREATE_TABLES_SQL)
        cur.execute(
            """
            SELECT atttypid = 'text'::regtype
            FROM pg_attribute
            WHERE attrelid = 'outcomes'::regclass AND attname = 'outcome_id'
            """
        )
        if not cur.fetchone()[0]:
            cur.execute(
                "ALTER TABLE outcomes ALTER COLUMN outcome_id TYPE TEXT USING outcome_id::text"
            )
    conn.commit()


def _to_db_values(df: pd.DataFrame) -> list[tuple]:
    """Turn a DataFrame into tuples psycopg2 can insert (pandas missing values -> None)."""
    clean = df.astype(object).where(df.notna(), None)
    return list(clean.itertuples(index=False, name=None))


def build_animals_table(df: pd.DataFrame) -> pd.DataFrame:
    """One row per animal. If an animal has several outcomes, keep its most recent row."""
    animals = (
        df.sort_values("outcome_date")
        .drop_duplicates(subset="animal_id", keep="last")
        .reindex(columns=list(ANIMAL_COLUMNS))
        .rename(columns=ANIMAL_COLUMNS)
    )
    animals["date_of_birth"] = pd.to_datetime(animals["date_of_birth"], errors="coerce").dt.date
    return animals


def build_outcomes_table(df: pd.DataFrame) -> pd.DataFrame:
    """One row per outcome event, with types matching the outcomes table."""
    outcomes = df.reindex(columns=list(OUTCOME_COLUMNS)).rename(columns=OUTCOME_COLUMNS)
    outcomes["outcome_id"] = outcomes["outcome_id"].astype("string")
    outcomes["days_in_shelter"] = pd.to_numeric(outcomes["days_in_shelter"], errors="coerce").astype("Int64")
    for col in ["outcome_date", "intake_date", "source_timestamp"]:
        outcomes[col] = pd.to_datetime(outcomes[col], errors="coerce")
    return outcomes.dropna(subset=["outcome_id"])


def upsert(conn, table: str, key: str, df: pd.DataFrame) -> dict[str, int]:
    """
    INSERT ... ON CONFLICT (key) DO UPDATE for every row in df.

    Rows that already exist with identical values are left alone.
    Returns counts of inserted, updated and unchanged rows.
    """
    if df.empty:
        return {"inserted": 0, "updated": 0, "unchanged": 0}

    cols = list(df.columns)
    non_key = [c for c in cols if c != key]
    col_list = ", ".join(cols)
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_key)
    changed = (
        f"({', '.join(f'{table}.{c}' for c in non_key)}) "
        f"IS DISTINCT FROM ({', '.join(f'EXCLUDED.{c}' for c in non_key)})"
    )

    sql = f"""
        INSERT INTO {table} ({col_list}) VALUES %s
        ON CONFLICT ({key}) DO UPDATE SET {set_clause}
        WHERE {changed}
        RETURNING (xmax = 0) AS inserted
    """

    with conn.cursor() as cur:
        results = execute_values(cur, sql, _to_db_values(df), page_size=1000, fetch=True)

    inserted = sum(1 for (was_insert,) in results if was_insert)
    updated = len(results) - inserted
    return {"inserted": inserted, "updated": updated, "unchanged": len(df) - len(results)}


def get_last_outcome_date(conn) -> datetime | None:
    """Latest outcome_date loaded by a previous run, or None on the first run."""
    with conn.cursor() as cur:
        cur.execute("SELECT last_outcome_date FROM etl_state WHERE source = %s", (SOURCE_NAME,))
        row = cur.fetchone()
    return row[0] if row else None


def update_etl_state(conn, last_outcome_date) -> None:
    """Record how far this run got so the next run knows where to start."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO etl_state (source, last_outcome_date, last_run_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (source) DO UPDATE
            SET last_outcome_date = GREATEST(etl_state.last_outcome_date, EXCLUDED.last_outcome_date),
                last_run_at = EXCLUDED.last_run_at
            """,
            (SOURCE_NAME, last_outcome_date),
        )


def run_load(df: pd.DataFrame) -> dict[str, dict[str, int]]:
    """Create tables, upsert animals then outcomes, update etl_state, all in one transaction."""
    animals = build_animals_table(df)
    outcomes = build_outcomes_table(df)

    conn = get_connection()
    try:
        create_tables(conn)
        stats = {
            "animals": upsert(conn, "animals", "animal_id", animals),
            "outcomes": upsert(conn, "outcomes", "outcome_id", outcomes),
        }
        latest = outcomes["outcome_date"].max()
        update_etl_state(conn, None if pd.isna(latest) else latest.to_pydatetime())
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    for table, counts in stats.items():
        print(
            f"  {table}: {counts['inserted']:,} inserted, "
            f"{counts['updated']:,} updated, {counts['unchanged']:,} unchanged"
        )
    return stats


if __name__ == "__main__":
    from transform import run_transform

    raw_file = Path(__file__).resolve().parent.parent / "data" / "raw" / "outcomes.json"
    cleaned_df, quality_stats = run_transform(raw_file)
    print("Data quality summary:")
    print(
    f"after cleaning: {quality_stats['clean_rows']:,} rows, "
    f"{quality_stats['duplicates_removed']:,} duplicates removed",
        flush=True,
    )

    fill_rules = {
        "name": "Unnamed",
        "outcome_subtype": "None",
        "sex_upon_outcome": "Unknown",
        "breed": "Unknown",
        "color": "Unknown",
    }

    missing_parts = []

    for column, count in quality_stats["missing_before_cleaning"].items():
        filled = quality_stats["missing_filled"].get(column, 0)

        if filled:
            replacement = fill_rules[column]
            missing_parts.append(
                f'{column} {count:,}, {filled:,} filled with "{replacement}"'
            )
        else:
            missing_parts.append(f"{column} {count:,}")

    print(f"missing values:\n{" "*16}" +  f",\n{" "*16}".join(missing_parts))

    run_load(cleaned_df)
