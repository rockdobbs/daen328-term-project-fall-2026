# Made with the help of AI as per the syllabus

import os
from datetime import timedelta
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError, SQLAlchemyError

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # Compose can supply the environment directly.

SOURCE_NAME = "austin_animal_outcomes"
INTAKES_API_URL = "https://data.austintexas.gov/resource/wter-evkm.json"
INTAKES_PAGE_SIZE = 50_000
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
    "age_at_exit": "Age at exit (years)",
    "age_at_intake": "Age at intake (years)",
}

# The Austin feed distinguishes kittens and puppies from adult cats and dogs.
SPECIES = {"cat": "Cats", "kitten": "Cats", "dog": "Dogs", "puppy": "Dogs"}
EXIT_GROUPS = {
    "adopted": "Adopted", "adoption": "Adopted",
    "reclaimed": "Returned to owner", "return to owner": "Returned to owner",
    "returned to owner": "Returned to owner",
}
AGE_BANDS = ["Under 6 mo", "6–11 mo", "1–2 yr", "3–6 yr", "7–9 yr", "10+ yr"]
STAY_BANDS = ["Same day", "1–7 days", "8–30 days", "31–90 days", "91+ days"]
TEAL, COPPER, BLUE = "#168477", "#C26A3D", "#50799B"
CARD_CHART_HEIGHT = 300


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
        return pd.read_sql_query(
            EVENTS_SQL, conn, params=params, parse_dates=["outcome_date", "intake_date"]
        )


@st.cache_data(ttl=3600, max_entries=1, show_spinner="Matching outcomes to animal intakes…")
def get_historical_intakes():
    """Download the two intake fields needed to reconstruct shelter stays."""
    pages = []
    offset = 0
    while True:
        query = urlencode({
            "$select": "animal_id,datetime",
            "$where": "animal_id is not null AND datetime is not null",
            "$order": "datetime,animal_id",
            "$limit": INTAKES_PAGE_SIZE,
            "$offset": offset,
        })
        request = Request(
            f"{INTAKES_API_URL}?{query}",
            headers={"Accept": "application/json", "User-Agent": "AustinAnimalDashboard/1.0"},
        )
        with urlopen(request, timeout=45) as response:
            records = json.load(response)
        if not records:
            break
        pages.append(pd.DataFrame.from_records(records))
        if len(records) < INTAKES_PAGE_SIZE:
            break
        offset += INTAKES_PAGE_SIZE

    if not pages:
        return pd.DataFrame(columns=["animal_id", "matched_intake_date"])
    intakes = pd.concat(pages, ignore_index=True).rename(
        columns={"datetime": "matched_intake_date"}
    )
    intakes["animal_id"] = intakes["animal_id"].astype("string").str.strip().str.upper()
    intakes["matched_intake_date"] = pd.to_datetime(
        intakes["matched_intake_date"], errors="coerce"
    )
    return (
        intakes.dropna(subset=["animal_id", "matched_intake_date"])
        .drop_duplicates(["animal_id", "matched_intake_date"])
        .sort_values(["matched_intake_date", "animal_id"])
        .reset_index(drop=True)
    )


def impute_shelter_stays(events, intakes):
    """Fill missing stays using the latest intake for that animal before its outcome."""
    data = events.copy()
    data["intake_date"] = pd.to_datetime(data["intake_date"], errors="coerce")
    recorded_stay = pd.to_numeric(data["days_in_shelter"], errors="coerce")
    data["days_in_shelter"] = recorded_stay.where(recorded_stay.ge(0))
    data["stay_imputed"] = False

    if data.empty or intakes.empty:
        return data

    left = data.reset_index(names="event_row")
    left["match_animal_id"] = left["animal_id"].astype("string").str.strip().str.upper()
    left = left.dropna(subset=["match_animal_id", "outcome_date"]).sort_values(
        ["outcome_date", "match_animal_id"]
    )
    right = intakes.rename(columns={"animal_id": "match_animal_id"}).sort_values(
        ["matched_intake_date", "match_animal_id"]
    )
    matched = pd.merge_asof(
        left,
        right,
        left_on="outcome_date",
        right_on="matched_intake_date",
        by="match_animal_id",
        direction="backward",
        allow_exact_matches=True,
    ).set_index("event_row")

    matched_intake = matched["matched_intake_date"].reindex(data.index)
    missing_intake = data["intake_date"].isna() & matched_intake.notna()
    data.loc[missing_intake, "intake_date"] = matched_intake.loc[missing_intake]

    calculated_stay = (
        data["outcome_date"].dt.normalize() - matched_intake.dt.normalize()
    ).dt.days
    can_impute = (
        data["days_in_shelter"].isna()
        & matched_intake.notna()
        & calculated_stay.ge(0)
    )
    data.loc[can_impute, "days_in_shelter"] = calculated_stay.loc[can_impute]
    data.loc[can_impute, "stay_imputed"] = True
    return data


def analysis_fields(events):
    """Keep one row per outcome and compute both ages from calendar dates."""
    data = events.copy()
    exited = data["outcome_date"]
    entered = pd.to_datetime(data["intake_date"], errors="coerce")
    born = pd.to_datetime(data["date_of_birth"], errors="coerce").dt.normalize()
    exit_days = (exited.dt.normalize() - born).dt.days
    intake_days = (entered.dt.normalize() - born).dt.days
    data["age_at_exit"] = exit_days.where(exit_days.ge(0)) / 365.25
    data["age_at_intake"] = intake_days.where(intake_days.ge(0) & entered.le(exited)) / 365.25
    stay = pd.to_numeric(data["days_in_shelter"], errors="coerce")
    data["stay"] = stay.where(stay.ge(0))
    data["breed"] = data["primary_breed"].astype("string").str.strip().replace("", pd.NA)
    data["species"] = data["animal_type"].str.strip().str.casefold().map(SPECIES)
    data["exit_group"] = data["outcome_status"].str.strip().str.casefold().map(EXIT_GROUPS)
    data["month"] = exited.dt.to_period("M").dt.to_timestamp()
    return data


def draw(fig, key, *, height=CARD_CHART_HEIGHT, grid="y", right=16):
    """Give every chart the same typography, spacing and theme-aware axes."""
    dark = getattr(getattr(st, "context", None), "theme", {}).get("type") == "dark"
    ink, rule = ("#EAF4F2", "#34484A") if dark else ("#233C42", "#DBE5E4")
    fig.update_layout(
        template="plotly_dark" if dark else "plotly_white",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, system-ui, sans-serif", color=ink, size=13),
        height=height, margin=dict(l=8, r=right, t=30, b=8),
        legend=dict(orientation="h", x=0, y=1.02, yanchor="bottom", title_text=""),
        hoverlabel=dict(bgcolor="#17383B", font_color="white", font_size=13),
        bargap=0.26,
    )
    for axis, update in (("x", fig.update_xaxes), ("y", fig.update_yaxes)):
        update(
            showgrid=axis in grid, gridcolor=rule, zeroline=False, ticks="",
            automargin=True, tickfont_size=12, title_font_size=12,
        )
    st.plotly_chart(
        fig, key=key, width="stretch", theme=None,
        config={"displaylogo": False, "modeBarButtonsToRemove": ["lasso2d", "select2d"]},
    )


def month_axis(fig, months):
    fig.update_xaxes(
        type="date", title=None, tickformat="%b<br>%Y", tick0=months[0],
        dtick=f"M{max(1, int(np.ceil(len(months) / 8)))}",
        range=[months[0] - pd.Timedelta(days=15), months[-1] + pd.Timedelta(days=16)],
    )
    fig.update_yaxes(rangemode="tozero", tickformat=",d")


def age_label(years):
    if pd.isna(years):
        return "N/A"
    return f"{years * 12:,.1f} mo" if years < 1 else f"{years:,.1f} yr"


def ranked_bars(counts, colors, key):
    fig = go.Figure(go.Bar(
        x=counts.values, y=counts.index, orientation="h", marker_color=colors,
        text=counts.values, texttemplate="%{text:,}", textposition="outside", cliponaxis=False,
        hovertemplate="%{y}<br>%{x:,} outcomes<extra></extra>",
    ))
    fig.update_yaxes(type="category", autorange="reversed")
    fig.update_xaxes(title="Outcome events", range=[0, max(1, counts.max() * 1.22)],
                     tickformat=",d", dtick=1 if counts.max() <= 6 else None)
    draw(fig, key, grid="x")


def overview(data, months):
    metrics = st.columns(4)
    metrics[0].metric("Outcome events", f"{len(data):,}", border=True)
    metrics[1].metric("Unique animals", f"{data['animal_id'].nunique():,}", border=True)
    metrics[2].metric(
        "Average age at exit", age_label(data["age_at_exit"].mean()), border=True,
        help="Mean age on the outcome date, across exits with a usable birth date.",
    )
    median = data["stay"].median() if data["stay"].notna().any() else np.nan
    imputed_count = int(data.get("stay_imputed", pd.Series(False, index=data.index)).sum())
    metrics[3].metric(
        "Median days in shelter",
        "N/A" if pd.isna(median) else f"{median:,.1f}",
        border=True,
        help=(
            "Missing stays are calculated from the most recent matching intake before the outcome. "
            f"{imputed_count:,} displayed stays were imputed from the Austin intake dataset."
        ),
    )
    if data.empty:
        st.info("No outcomes match these filters.")
        return

    status_counts = data["outcome_status"].value_counts()
    stays = data.dropna(subset=["stay"])
    stay_by_type = stays.groupby("animal_type")["stay"]
    stay_summary = stay_by_type.agg(outcomes="size", median="median").sort_values(
        ["outcomes", "median"], ascending=False
    )

    left, right = st.columns(2, border=True)
    with left:
        st.subheader("Outcomes by month")
        counts = data.groupby("month").size().reindex(months, fill_value=0)
        fig = go.Figure(go.Scatter(
            x=months, y=counts, mode="lines+markers", line=dict(color=BLUE, width=3),
            marker=dict(size=7), fill="tozeroy", fillcolor="rgba(80,121,155,0.14)",
            hovertemplate="%{x|%B %Y}<br>%{y:,} outcome events<extra></extra>",
        ))
        month_axis(fig, months)
        fig.update_yaxes(title="Outcome events", dtick=1 if counts.max() <= 6 else None)
        draw(fig, "all_months")
    with right:
        st.subheader(
            "Outcomes by status",
            help="Shows the most frequent statuses. The full list is below the charts.",
        )
        counts = status_counts
        if len(status_counts) > 8:
            remaining = status_counts.iloc[7:]
            counts = pd.concat([
                status_counts.head(7),
                pd.Series({f"Other statuses ({len(remaining)})": remaining.sum()}),
            ])
        palette = {"Adopted": TEAL, "Returned to owner": COPPER}
        colors = [palette.get(EXIT_GROUPS.get(s.strip().casefold()), BLUE) for s in counts.index]
        ranked_bars(counts, colors, "status_counts")

    left, right = st.columns(2, border=True)
    with left:
        st.subheader(
            "Days in shelter by animal type",
            help="Shows up to 10 animal types with the most recorded stays, ordered by median stay.",
        )
        if stays.empty:
            st.info("No shelter stays were recorded for this selection.")
        else:
            fig = go.Figure()
            order = stay_summary.head(10).sort_values("median", ascending=False).index
            for kind in order:
                values = stay_by_type.get_group(kind)
                fig.add_trace(go.Box(
                    x=values, name=kind, orientation="h", showlegend=False,
                    line_color=BLUE, fillcolor="rgba(80,121,155,0.24)",
                    marker=dict(color=BLUE, size=4, opacity=0.5), boxpoints="outliers",
                ))
            fig.update_yaxes(type="category", autorange="reversed")
            fig.update_xaxes(title="Days in shelter", rangemode="tozero")
            draw(fig, "stay_types", grid="x")
    with right:
        st.subheader("Top 10 breeds by number of outcome events")
        counts = data["breed"].value_counts().head(10)
        if counts.empty:
            st.info("No primary breeds were recorded for this selection.")
        else:
            ranked_bars(counts, BLUE, "breed_counts")

    with st.expander("View outcome records"):
        detail = data.loc[:, list(LABELS)].rename(columns=LABELS)
        st.dataframe(detail, width="stretch", hide_index=True, column_config={
            LABELS[col]: st.column_config.NumberColumn(format="%.2f")
            for col in ("age_at_exit", "age_at_intake")
        })

    if len(status_counts) > 8 or len(stay_summary) > 10:
        with st.expander("Full status and animal type breakdown"):
            left, right = st.columns(2)
            with left:
                st.markdown("**Outcomes by status**")
                st.dataframe(
                    status_counts.rename_axis("Outcome status").reset_index(name="Outcome events"),
                    hide_index=True,
                )
            with right:
                st.markdown("**Recorded stays by animal type**")
                st.dataframe(
                    stay_summary.rename_axis("Animal type").rename(columns={
                        "outcomes": "Recorded stays", "median": "Median days",
                    }).reset_index(),
                    hide_index=True,
                )


def adoption_views(data, months):
    st.caption("These comparisons use every outcome status for the selected animal types and dates. "
               "The first and last months include only the selected days.")
    adopted = data.loc[data["exit_group"].eq("Adopted")]
    left, right = st.columns(2, border=True)
    with left:
        st.subheader("Adoptions per month")
        counts = adopted.groupby("month").size().reindex(months, fill_value=0)
        fig = go.Figure(go.Bar(
            x=months, y=counts, marker_color=TEAL, width=21 * 86_400_000,  # 21 days in milliseconds
            hovertemplate="%{x|%B %Y}<br>%{y:,} adoptions<extra></extra>",
        ))
        if len(months) <= 12:
            fig.update_traces(text=counts, texttemplate="%{text:,}", textposition="outside", cliponaxis=False)
        month_axis(fig, months)
        fig.update_yaxes(title="Adoption events", range=[0, max(1, counts.max() * 1.2)],
                         dtick=1 if counts.max() <= 6 else None)
        draw(fig, "adopt_months")

    with right:
        st.subheader(
            "Adoption rate by species",
            help="Each rate uses all outcomes for that species. Within the selected types, "
                 "kittens count with cats and puppies with dogs.",
        )
        species = pd.Index(["Cats", "Dogs"], name="species")
        totals = data.groupby("species").size().reindex(species, fill_value=0)
        adoptions = adopted.groupby("species").size().reindex(species, fill_value=0)
        rates = adoptions.div(totals.where(totals.gt(0)))
        fig = go.Figure()
        hover_data = np.column_stack([adoptions, totals, rates])
        hover = ("%{y}<br>%{customdata[0]:,.0f} adoptions / %{customdata[1]:,.0f} outcomes"
                 "<br>%{customdata[2]:.1%} adopted<extra></extra>")
        fig.add_bar(x=np.where(totals.gt(0), 1.0, np.nan), y=species, orientation="h",
                    marker_color="rgba(120,151,147,0.23)", width=0.46,
                    customdata=hover_data, hovertemplate=hover, showlegend=False)
        fig.add_bar(x=rates, y=species, orientation="h", marker_color=TEAL, width=0.46,
                    customdata=hover_data, hovertemplate=hover, showlegend=False)
        for group, rate in rates.items():
            fig.add_annotation(
                x=1.025, xref="paper", y=group, text="N/A" if pd.isna(rate) else f"{rate:.1%}",
                xanchor="left", showarrow=False, font_size=14,
            )
        fig.update_layout(barmode="overlay")
        fig.update_xaxes(range=[0, 1], tickformat=".0%", dtick=0.25, title="Share of recorded outcomes")
        fig.update_yaxes(type="category", autorange="reversed", categoryorder="array",
                         categoryarray=list(species), tickmode="array", tickvals=list(species),
                         ticktext=[f"{group}<br>n = {totals[group]:,}" for group in species])
        draw(fig, "species_rates", grid="x", right=62)

    with st.container(border=True):
        st.subheader("Animals adopted or returned to their owners")
        st.caption("Each animal counts once per month in each series. Austin records owner returns as Reclaimed.")
        # An animal may have more than one outcome event in a month.
        distinct = data.dropna(subset=["exit_group"]).groupby(["month", "exit_group"])["animal_id"].nunique()
        counts = distinct.unstack(fill_value=0).reindex(
            index=months, columns=["Adopted", "Returned to owner"], fill_value=0,
        )
        fig = go.Figure()
        for label, color, dash, symbol in [
            ("Adopted", TEAL, "solid", "circle"),
            ("Returned to owner", COPPER, "dash", "diamond"),
        ]:
            fig.add_scatter(
                x=months, y=counts[label], name=label, mode="lines+markers",
                line=dict(color=color, width=3, dash=dash), marker=dict(size=8, symbol=symbol),
                hovertemplate="%{y:,} animals<extra>%{fullData.name}</extra>",
            )
        month_axis(fig, months)
        fig.update_layout(hovermode="x unified")
        peak = counts.to_numpy().max()
        fig.update_yaxes(title="Unique animals", range=[0, max(1, peak * 1.1)], dtick=1 if peak <= 6 else None)
        draw(fig, "adopt_returns", height=325)


def age_distribution(data):
    st.subheader("Age at exit")
    ages = data["age_at_exit"].dropna()
    if ages.empty:
        st.info("No outcomes in this selection have a usable birth date.")
        return
    mean = ages.mean()
    step = 0.25 if ages.max() < 4 else max(1.0, np.ceil(ages.max() / 24))
    edges = np.arange(int(ages.max() // step) + 2) * step
    counts, _ = np.histogram(ages, bins=edges)
    fig = go.Figure(go.Bar(
        x=(edges[1:] + edges[:-1]) / 2, y=counts, width=step * 0.9, marker_color=BLUE,
        customdata=np.column_stack([edges[:-1], edges[1:]]),
        hovertemplate=("%{customdata[0]:g}–%{customdata[1]:g} years<br>"
                       "%{y:,} outcome events<extra></extra>"),
    ))
    fig.add_vline(x=mean, line_color=COPPER, line_width=2.5, line_dash="dash")
    fig.add_annotation(x=mean, y=1, yref="paper", yshift=6, yanchor="bottom",
                       xanchor="left" if mean < edges[-1] / 2 else "right",
                       text=f"Mean {age_label(mean)}", showarrow=False)
    fig.update_xaxes(title="Age at exit (years)", range=[0, edges[-1]])
    fig.update_yaxes(title="Outcome events", rangemode="tozero", tickformat=",d",
                     dtick=1 if counts.max() <= 6 else None)
    draw(fig, "age_at_exit")
    st.caption(f"Average {age_label(mean)} across {len(ages):,} of {len(data):,} outcomes. "
               "Age is measured on the outcome date.")


def breed_stays(data):
    st.subheader("Length of stay by primary breed")
    stays = data.dropna(subset=["breed", "stay"])
    if stays.empty:
        st.info("No outcomes have both a recorded breed and shelter stay.")
        return
    common = stays["breed"].value_counts().head(10).index
    grouped = stays.loc[stays["breed"].isin(common)].groupby("breed")["stay"]
    summary = grouped.agg(["size", "median"])
    summary["q1"], summary["q3"] = grouped.quantile(0.25), grouped.quantile(0.75)
    summary = summary.sort_values(["median", "size"], ascending=False)
    fig = go.Figure(go.Scatter(
        x=summary["median"], y=summary.index, mode="markers", marker=dict(size=10, color=TEAL),
        error_x=dict(type="data", symmetric=False, array=summary["q3"] - summary["median"],
                     arrayminus=summary["median"] - summary["q1"], color=TEAL, thickness=2, width=4),
        customdata=summary[["size", "q1", "q3"]].to_numpy(),
        hovertemplate=("%{y}<br>Median %{x:g} days<br>Middle 50% %{customdata[1]:g}–%{customdata[2]:g} days"
                       "<br>%{customdata[0]:,.0f} outcomes<extra></extra>"),
    ))
    fig.update_yaxes(type="category", autorange="reversed")
    fig.update_xaxes(title="Days in shelter", rangemode="tozero")
    draw(fig, "breed_stays", grid="x")
    st.caption("For the 10 most frequent breeds with a recorded stay, dots mark the median. "
               "Lines span the middle 50% of stays.")


def age_and_stay(data):
    st.subheader("Age at intake and length of stay")
    pairs = data.dropna(subset=["age_at_intake", "stay"])
    if pairs.empty:
        st.info("No outcomes have a usable birth date, intake date, and shelter stay together.")
        return
    breeds = sorted(pairs["breed"].dropna().unique())
    chosen = st.selectbox(
        "Breed for the age and stay comparison", [None, *breeds],
        format_func=lambda value: "All breeds" if value is None else value, key="age_stay_breed",
    )
    if chosen is not None:
        pairs = pairs.loc[pairs["breed"].eq(chosen)]
    n = len(pairs)
    if n >= 3 and pairs[["age_at_intake", "stay"]].nunique().min() > 1:
        rho = pairs["age_at_intake"].rank().corr(pairs["stay"].rank())
        st.markdown(f"**Spearman ρ = {rho:+.2f}** across {n:,} outcomes.")
        st.caption("A positive value means older arrivals tended to stay longer. "
                   "A negative value means they tended to leave sooner.")
    else:
        st.caption(f"{n:,} paired outcomes. Reporting a rank correlation requires at least three "
                   "observations and variation in both age and stay.")
    age = pd.cut(pairs["age_at_intake"], [0, 0.5, 1, 3, 7, 10, np.inf], right=False, labels=AGE_BANDS)
    stay = pd.cut(pairs["stay"], [-1, 0, 7, 30, 90, np.inf], labels=STAY_BANDS)
    counts = pd.crosstab(stay, age, dropna=False).reindex(index=STAY_BANDS, columns=AGE_BANDS, fill_value=0)
    totals = counts.sum()
    shares = counts.div(totals.where(totals.gt(0)), axis="columns")
    denominators = np.broadcast_to(totals.to_numpy(), counts.shape)
    fig = go.Figure(go.Heatmap(
        x=AGE_BANDS, y=STAY_BANDS, z=shares.to_numpy(), zmin=0, zmax=1, showscale=False,
        colorscale=[[0, "#EAF2F0"], [0.35, "#A6D1C6"], [0.65, "#398F83"], [1, "#10584F"]],
        customdata=np.stack([counts.to_numpy(), denominators], axis=-1),
        xgap=3, ygap=3, hoverongaps=False,
        hovertemplate=("Age %{x}<br>Stay %{y}<br>%{customdata[0]:,} of %{customdata[1]:,} "
                       "outcomes (%{z:.1%})<extra></extra>"),
    ))
    for age_band in AGE_BANDS:
        for stay_band, share in shares[age_band].items():
            if pd.notna(share):
                fig.add_annotation(x=age_band, y=stay_band, text=f"{share:.0%}", showarrow=False,
                                   font=dict(size=12, color="white" if share >= 0.75 else "#061412"))
    fig.update_xaxes(title="Age at intake", type="category", categoryorder="array", categoryarray=AGE_BANDS)
    fig.update_yaxes(title="Days in shelter", type="category", categoryorder="array", categoryarray=STAY_BANDS)
    draw(fig, "age_stay_heatmap", height=330, grid="")
    st.caption("Each column shows how stays are distributed within an age group. Blank columns have no observations.")


def show_results(data, selected_statuses, start_date, end_date):
    months = pd.date_range(start_date.replace(day=1), end_date.replace(day=1), freq="MS", name="month")
    selected = data.loc[data["outcome_status"].isin(selected_statuses)]
    general, adoption, ages = st.tabs(["Overview", "Adoptions & returns", "Age & shelter stays"])
    with general:
        overview(selected, months)
    with adoption:
        adoption_views(data, months)
    with ages:
        left, right = st.columns(2, border=True)
        with left:
            age_distribution(selected)
        with right:
            breed_stays(selected)
        with st.container(border=True):
            age_and_stay(selected)


def main():
    st.set_page_config(page_title="Austin Animal Center Outcomes", layout="wide")
    st.html("""
        <style>
        [data-testid="stMainBlockContainer"] { max-width: 88rem; }
        [data-testid="stMetric"] { border-top: 3px solid #168477; }
        [data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; }
        </style>
    """)
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
    selected_statuses = st.sidebar.multiselect(
        "Outcome status", statuses, default=statuses,
        help="Applies to Overview and Age & shelter stays. Adoptions & returns uses every status.",
    )

    first_day = options["first_outcome"].min().date()
    last_day = options["last_outcome"].max().date()
    dates = st.sidebar.date_input(
        "Outcome date range", value=(first_day, last_day), min_value=first_day, max_value=last_day,
    )
    if not selected_types:
        st.info("Select at least one animal type.")
        return
    if not dates or len(dates) != 2:
        st.info("Select a start date and an end date.")
        return

    start_date, end_date = dates
    # Fetch the full denominator once. The overview and age views apply the status filter locally.
    events = get_events(selected_types, statuses, start_date, end_date, last_run_at)
    needs_intake_match = (
        pd.to_numeric(events["days_in_shelter"], errors="coerce").isna()
        | pd.to_datetime(events["intake_date"], errors="coerce").isna()
    ).any()
    intakes = pd.DataFrame(columns=["animal_id", "matched_intake_date"])
    if needs_intake_match:
        try:
            intakes = get_historical_intakes()
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            st.warning(
                "The historical intake feed could not be reached, so this view is using only "
                "shelter stays already stored in the outcomes database."
            )
    events = impute_shelter_stays(events, intakes)
    show_results(analysis_fields(events), selected_statuses, start_date, end_date)


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
