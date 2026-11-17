import pandas as pd
import pytest
from transform import (
    handle_missing_values,
    normalize_text_columns,
    parse_age_to_days,
    parse_dates,
    remove_duplicates,
)


def test_remove_duplicates():
    raw = pd.DataFrame({
        "id": ["101", "101", "102"],
        "name": ["Bella", "Bella Updated", "Max"],
    })
    cleaned, removed = remove_duplicates(raw, subset_col="id")

    assert removed == 1
    assert len(cleaned) == 2
    assert cleaned.loc[cleaned["id"] == "101", "name"].iloc[0] == "Bella Updated"


def test_parse_dates():
    raw = pd.DataFrame({
        "outcome_date": ["2026-10-15T14:30:00.000", "invalid-date"],
    })
    cleaned = parse_dates(raw, date_cols=["outcome_date"])

    assert pd.api.types.is_datetime64_any_dtype(cleaned["outcome_date"])
    assert cleaned["outcome_date"].iloc[0] == pd.Timestamp("2026-10-15 14:30:00")
    assert pd.isna(cleaned["outcome_date"].iloc[1])


def test_normalize_text_columns():
    raw = pd.DataFrame({
        "breed": ["  labrador retriever mix ", "domestic SHORTHAIR"],
        "color": ["black/white", "  TAN  "],
    })
    cleaned = normalize_text_columns(raw, cols=["breed", "color"])

    assert cleaned["breed"].tolist() == ["Labrador Retriever Mix", "Domestic Shorthair"]
    assert cleaned["color"].tolist() == ["Black/White", "Tan"]


def test_parse_age_to_days():
    raw = pd.DataFrame({
        "age_upon_outcome": ["2 years", "3 months", "2 weeks", "5 days", "unknown"],
    })
    cleaned = parse_age_to_days(raw, age_col="age_upon_outcome")

    assert cleaned["age_days"].iloc[0] == 730
    assert cleaned["age_days"].iloc[1] == 91
    assert cleaned["age_days"].iloc[2] == 14
    assert cleaned["age_days"].iloc[3] == 5
    assert pd.isna(cleaned["age_days"].iloc[4])


def test_handle_missing_values():
    raw = pd.DataFrame({
        "id": ["1", "2", "3"],
        "animal_id": ["A1", "A2", None],  # Row 3 missing critical foreign key -> should drop
        "outcome_date": ["2026-10-01", "2026-10-02", "2026-10-03"],
        "name": [None, "   ", "Charlie"],
        "breed": ["Pug", None, "Beagle"],
    })
    cleaned, fill_counts = handle_missing_values(raw)

    # Row 3 should be dropped due to missing animal_id
    assert len(cleaned) == 2
    assert "3" not in cleaned["id"].values

    # Missing/blank names and breeds should be filled before row 3 is dropped
    assert fill_counts["name"] == 2
    assert fill_counts["breed"] == 1
    assert cleaned["name"].tolist() == ["Unnamed", "Unnamed"]
    assert cleaned["breed"].iloc[1] == "Unknown"