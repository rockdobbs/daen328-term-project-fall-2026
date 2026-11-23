# Made with the help of AI as per the syllabus

import os
from datetime import timedelta

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError, SQLAlchemyError

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # Compose can supply the environment directly.

SOURCE_NAME = "austin_animal_outcomes"
DB_SETTINGS = (
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"
)

FILTER_OPTIONS_SQL = text("""
    SELECT a.animal_type, o.outcome_status,
           MIN(o.outcome_date) AS first_outcome, MAX(o.outcome_date) AS last_outcome
    FROM outcomes AS o
    JOIN animals AS a ON a.animal_id = o.animal_id
    GROUP BY a.animal_type, o.outcome_status
""")

EVENTS_SQL = text("""
    SELECT o.outcome_id, o.outcome_date, o.outcome_status, a.animal_id,
           a.name AS animal_name, a.animal_type, a.primary_breed, o.days_in_shelter,
           o.intake_date, a.sex, o.spayed_neutered, a.date_of_birth,
           a.primary_color, a.secondary_color, o.euthanasia_reason, o.source_timestamp
    FROM outcomes AS o
    JOIN animals AS a ON a.animal_id = o.animal_id
    WHERE a.animal_type IN :animal_types AND o.outcome_status IN :statuses
      AND o.outcome_date >= :start_date AND o.outcome_date < :end_exclusive
    ORDER BY o.outcome_date DESC, o.outcome_id DESC
""").bindparams(
    bindparam("animal_types", expanding=True),
    bindparam("statuses", expanding=True),
)

LABELS = {
    "outcome_id": "Outcome ID",
    "outcome_date": "Outcome date",
    "outcome_status": "Outcome status",
    "animal_id": "Animal ID",
    "animal_name": "Name",
    "animal_type": "Animal type",
    "primary_breed": "Primary breed",
    "days_in_shelter": "Days in shelter",
    "intake_date": "Intake date",
    "sex": "Sex",
    "spayed_neutered": "Spayed/neutered",
    "date_of_birth": "Date of birth",
    "primary_color": "Primary color",
    "secondary_color": "Secondary color",
    "euthanasia_reason": "Euthanasia reason",
    "source_timestamp": "Source timestamp",
    "month": "Outcome month",
    "event_count": "Outcome events",
}


@st.cache_resource(show_spinner=False)
def get_engine():
    if load_dotenv is not None:
        load_dotenv()
    missing = [name for name in DB_SETTINGS if name not in os.environ]
    if missing:
        raise ValueError(
            f"The dashboard service is missing {', '.join(missing)}. "
            "Give it the same PostgreSQL settings as the loader and restart it."
        )
    try:
        port = int(os.environ["POSTGRES_PORT"])
    except ValueError:
        raise ValueError("POSTGRES_PORT must be a port number.") from None
    url = URL.create(
        "postgresql+psycopg2",
        username=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        host=os.environ["POSTGRES_HOST"],
        port=port,
        database=os.environ["POSTGRES_DB"],
    )
    return create_engine(url, pool_pre_ping=True)


# last_run_at is part of both cache keys. The loader updates it when its writes commit.
@st.cache_data(ttl=600, max_entries=2, show_spinner=False)
def get_filter_options(last_run_at):
    with get_engine().connect() as conn:
        return pd.read_sql_query(
            FILTER_OPTIONS_SQL, conn, parse_dates=["first_outcome", "last_outcome"]
        )


@st.cache_data(ttl=600, max_entries=8, show_spinner=False)
def get_events(animal_types, statuses, start_date, end_date, last_run_at):
    params = {
        "animal_types": animal_types,
        "statuses": statuses,
        "start_date": start_date,
        "end_exclusive": end_date + timedelta(days=1),
    }
    with get_engine().connect() as conn:
        return pd.read_sql_query(EVENTS_SQL, conn, params=params, parse_dates=["outcome_date"])


def show_results(events, start_date, end_date):
    stays = events.dropna(subset=["days_in_shelter"])
    totals, animals, length = st.columns(3)
    totals.metric("Outcome events", f"{len(events):,}")
    animals.metric(
        "Unique animals", f"{events['animal_id'].nunique():,}",
        help="Each animal ID counts once in the selected outcomes.",
    )
    median_days = "N/A" if stays.empty else f"{stays['days_in_shelter'].median():,.1f}"
    length.metric("Median days in shelter", median_days, help="Uses outcomes with a recorded stay.")

    if events.empty:
        st.info("No outcomes match these filters.")
        return

    trend, status = st.columns(2)
    with trend:
        st.subheader("Outcomes by month")
        months = pd.date_range(
            start_date.replace(day=1), end_date.replace(day=1), freq="MS", name="month"
        )
        monthly = events.groupby(pd.Grouper(key="outcome_date", freq="MS")).size()
        monthly = monthly.reindex(months, fill_value=0).rename("event_count").reset_index()
        fig = px.line(monthly, x="month", y="event_count", markers=True, labels=LABELS)
        fig.update_yaxes(rangemode="tozero")
        st.plotly_chart(fig, width="stretch")

    with status:
        st.subheader("Outcomes by status")
        counts = events["outcome_status"].value_counts().rename_axis("outcome_status")
        counts = counts.rename("event_count").reset_index()
        fig = px.bar(counts, x="event_count", y="outcome_status", orientation="h", labels=LABELS)
        fig.update_yaxes(autorange="reversed")
        st.plotly_chart(fig, width="stretch")

    duration, breed = st.columns(2)
    with duration:
        st.subheader("Days in shelter by animal type")
        if stays.empty:
            st.info("None of these outcomes has a recorded shelter stay.")
        else:
            fig = px.box(
                stays, x="animal_type", y="days_in_shelter", points="outliers", labels=LABELS
            )
            st.plotly_chart(fig, width="stretch")

    with breed:
        st.subheader("Top 10 breeds by outcome events")
        counts = events["primary_breed"].replace("", None).value_counts().head(10)
        if counts.empty:
            st.info("None of these outcomes has a recorded breed.")
        else:
            top_breeds = counts.rename_axis("primary_breed").rename("event_count").reset_index()
            fig = px.bar(
                top_breeds, x="event_count", y="primary_breed", orientation="h", labels=LABELS
            )
            fig.update_yaxes(autorange="reversed")
            st.plotly_chart(fig, width="stretch")

    with st.expander("View outcome records"):
        st.dataframe(events.rename(columns=LABELS), width="stretch", hide_index=True)


def main():
    st.set_page_config(page_title="Austin Animal Center Outcomes", layout="wide")
    st.title("Austin Animal Center Outcomes")
    st.sidebar.button("Refresh data", help="Check for a newly completed load.")

    try:
        engine = get_engine()
    except ValueError as exc:
        st.error(str(exc))
        return

    with engine.connect() as conn:
        last_run_at = conn.execute(
            text("SELECT last_run_at FROM etl_state WHERE source = :source"),
            {"source": SOURCE_NAME},
        ).scalar_one_or_none()
    if last_run_at is not None:
        st.caption(f"Last completed load · {pd.Timestamp(last_run_at):%b %d, %Y at %H:%M}")

    options = get_filter_options(last_run_at)
    if options.empty:
        st.info("No outcomes have been loaded yet. Refresh after the pipeline finishes.")
        return

    st.sidebar.header("Filters")
    types = sorted(options["animal_type"].unique())
    statuses = sorted(options["outcome_status"].unique())
    selected_types = st.sidebar.multiselect("Animal type", types, default=types)
    selected_statuses = st.sidebar.multiselect("Outcome status", statuses, default=statuses)

    first_day = options["first_outcome"].min().date()
    last_day = options["last_outcome"].max().date()
    dates = st.sidebar.date_input(
        "Outcome date range", value=(first_day, last_day), min_value=first_day, max_value=last_day,
    )
    if not selected_types or not selected_statuses:
        st.info("Select at least one animal type and one outcome status.")
        return
    if not dates or len(dates) != 2:
        st.info("Select a start date and an end date.")
        return

    start_date, end_date = dates
    events = get_events(selected_types, selected_statuses, start_date, end_date, last_run_at)
    show_results(events, start_date, end_date)


if __name__ == "__main__":
    try:
        main()
    except OperationalError:
        st.error(
            "Could not connect to PostgreSQL. Check that the database is running and that "
            "the app and loader use the same PostgreSQL connection settings."
        )
    except (SQLAlchemyError, pd.errors.DatabaseError) as exc:
        db_error = exc.__cause__ if isinstance(exc, pd.errors.DatabaseError) else exc
        if getattr(getattr(db_error, "orig", None), "pgcode", None) == "42P01":
            st.error(
                "The pipeline tables are missing. Check the selected database and refresh "
                "after the first successful load."
            )
        else:
            st.error(
                "Could not read the outcome data. Check that the dashboard database user "
                "can read animals, outcomes, and etl_state."
            )
