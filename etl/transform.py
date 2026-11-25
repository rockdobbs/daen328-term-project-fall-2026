import json
from pathlib import Path
import pandas as pd


def load_raw_data(filepath: str | Path) -> pd.DataFrame:
    """Load raw JSON records into a pandas DataFrame."""
    with open(filepath, encoding="utf-8") as f:
        records = json.load(f)
    return pd.DataFrame(records)


def remove_duplicates(df: pd.DataFrame, subset_col: str = "id") -> tuple[pd.DataFrame, int]:
    """Remove duplicate records based on a unique identifier and return count removed."""
    initial_count = len(df)
    df_clean = df.drop_duplicates(subset=[subset_col], keep="last").copy()
    removed_count = initial_count - len(df_clean)
    return df_clean, removed_count


def parse_dates(df: pd.DataFrame, date_cols: list[str]) -> pd.DataFrame:
    """Convert ISO string timestamps to timezone-naive datetime objects."""
    df_clean = df.copy()
    for col in date_cols:
        if col in df_clean.columns:
            # Keep Austin's wall time for PostgreSQL TIMESTAMP columns.
            local_time = df_clean[col].astype("string").str.replace(
                r"(?:Z|[+-]\d{2}:?\d{2})$", "", regex=True
            )
            df_clean[col] = pd.to_datetime(local_time, format="ISO8601", errors="coerce")
    return df_clean


def normalize_text_columns(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Strip whitespace and apply title casing to categorical text columns."""
    df_clean = df.copy()
    for col in cols:
        if col in df_clean.columns:
            df_clean[col] = (
                df_clean[col]
                .astype("string")
                .str.strip()
                .str.title()
            )
    return df_clean


def parse_age_to_days(df: pd.DataFrame, age_col: str = "age_upon_outcome") -> pd.DataFrame:
    """Convert string ages (e.g., '2 years', '3 months') into a numeric age_days column."""
    df_clean = df.copy()
    if age_col not in df_clean.columns:
        return df_clean

    def _to_days(val):
        if pd.isna(val) or not isinstance(val, str):
            return pd.NA
        parts = val.strip().lower().split()
        if len(parts) != 2:
            return pd.NA
        try:
            num = float(parts[0])
        except ValueError:
            return pd.NA

        unit = parts[1]
        if "day" in unit:
            return round(num)
        elif "week" in unit:
            return round(num * 7)
        elif "month" in unit:
            return round(num * 30.44)
        elif "year" in unit:
            return round(num * 365.25)
        return pd.NA

    df_clean["age_days"] = df_clean[age_col].apply(_to_days).astype("Int64")
    return df_clean


def handle_missing_values(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """
    Handle missing values column by column rather than dropping rows blindly.
    Returns the cleaned DataFrame and a dictionary of filled counts per column.
    """
    df_clean = df.copy()
    fill_counts = {}

    # Categorical columns where missing is meaningful (e.g., unnamed stray animals)
    fill_rules = {
        "name": "Unnamed",
        "outcome_subtype": "None",
        "sex_upon_outcome": "Unknown",
        "breed": "Unknown",
        "color": "Unknown",
    }

    for col, fill_val in fill_rules.items():
        if col in df_clean.columns:
            # Treat empty strings or <NA> as missing
            missing_mask = df_clean[col].isna() | (df_clean[col].astype(str).str.strip() == "")
            fill_counts[col] = int(missing_mask.sum())
            df_clean.loc[missing_mask, col] = fill_val

    # Drop rows missing critical primary/foreign key or timestamp fields
    critical_cols = [c for c in ["id", "animal_id", "outcome_date"] if c in df_clean.columns]
    df_clean = df_clean.dropna(subset=critical_cols)

    return df_clean, fill_counts


def run_transform(raw_path: str | Path) -> tuple[pd.DataFrame, dict]:
    """Orchestrate all cleaning functions and collect data quality metrics."""
    df = load_raw_data(raw_path)
    stats = {"raw_rows": len(df)}
    stats["missing_before_cleaning"] = (
      df.replace(r"^\s*$", pd.NA, regex=True)
      .isna()
      .sum()
      .astype(int)
      .to_dict()
    )
    if "outcome_date" not in df.columns and "datetime" in df.columns:
        df["outcome_date"] = df["datetime"]

    df, dupes_removed = remove_duplicates(df, subset_col="id")
    stats["duplicates_removed"] = dupes_removed

    df = normalize_text_columns(
        df, cols=["name", "animal_type", "breed", "color", "outcome_type", "outcome_subtype", "sex_upon_outcome"]
    )
    df = parse_dates(df, date_cols=["outcome_date", "date_of_birth"])
    df = parse_age_to_days(df, age_col="age_upon_outcome")
    df, fill_counts = handle_missing_values(df)

    # Map the archive fields to the names used by the existing loader.
    for target, source in {
        "type": "animal_type",
        "sex": "sex_upon_outcome",
        "primary_breed": "breed",
        "primary_color": "color",
        "outcome_status": "outcome_type",
    }.items():
        if target not in df.columns and source in df.columns:
            df[target] = df[source]

    for col in ("type", "outcome_status"):
        values = df.reindex(columns=[col])[col].astype("string").str.strip()
        df[col] = values.replace("", pd.NA).fillna("Unknown")

    stats["clean_rows"] = len(df)
    stats["missing_filled"] = fill_counts

    stats["missing_after_cleaning"] = (
        df.replace(r"^\s*$", pd.NA, regex=True)
        .isna()
        .sum()
        .astype(int)
        .to_dict()
    )

    return df, stats


if __name__ == "__main__":
    raw_file = Path(__file__).resolve().parent.parent / "data" / "raw" / "outcomes.json"
    cleaned_df, quality_stats = run_transform(raw_file)
    print("Transform Summary:", quality_stats)
    print(cleaned_df.head())
