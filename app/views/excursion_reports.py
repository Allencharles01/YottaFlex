"""Excursion Reports: a filterable table; each report opens its readings and its policy."""

import streamlit as st

from coldchain.enums import TableName
from coldchain.viewer import session, views

TEMPERATURE_PAGE = "views/temperature_chart.py"

reports = views.excursion_table(
    session.table(TableName.EXCURSION_REPORTS), session.table(TableName.COMPLIANCE_DECISIONS)
)

st.title("Excursion Reports")
if reports.empty:
    st.info("The compliance engine wrote no excursion reports for this scale.")
    st.stop()

columns = st.columns(4)
direction = columns[0].multiselect(
    "Direction", sorted(reports["direction"].unique()), key="exc_direction", placeholder="All"
)
breach = columns[1].multiselect(
    "Breach type", sorted(reports["breach_type"].unique()), key="exc_breach", placeholder="All"
)
policy_ids = columns[2].multiselect(
    "Policy", sorted(reports["policy_id"].unique()), key="exc_policy", placeholder="All"
)
search = columns[3].text_input("Shipment or report ID contains", key="exc_search").strip()

shown = reports
for column, chosen in (
    ("direction", direction),
    ("breach_type", breach),
    ("policy_id", policy_ids),
):
    if chosen:
        shown = shown[shown[column].isin(chosen)]
if search:
    needle = search.upper()
    shown = shown[
        shown["shipment_id"].str.contains(needle, regex=False)
        | shown["report_id"].str.contains(needle, regex=False)
    ]
st.caption(f"{len(shown):,} of {len(reports):,} reports.")
st.dataframe(
    shown[
        [
            "report_id",
            "shipment_id",
            "policy_id",
            "direction",
            "started_at",
            "ended_at",
            "duration_min",
            "peak_c",
            "reading_count",
            "breach_type",
            "shipment_status",
        ]
    ],
    hide_index=True,
)
if shown.empty:
    st.stop()

report_id = st.selectbox("Report details", sorted(shown["report_id"]), key="exc_report")
report = views.one_row(shown, "report_id", report_id)

st.header(report_id)
columns = st.columns(4)
columns[0].metric("Breach type", report["breach_type"])
columns[1].metric("Duration", f"{report['duration_min']} min")
columns[2].metric("Peak", f"{report['peak_c']:g} °C")
columns[3].metric("Shipment status", report["shipment_status"])
if st.button(
    f"Open {report['shipment_id']} in the temperature chart",
    icon=":material/thermostat:",
    key="exc_chart",
):
    session.open_shipment(report["shipment_id"], TEMPERATURE_PAGE)

st.subheader(f"Policy {report['policy_id']}")
policies = session.table(TableName.THRESHOLD_POLICIES)
st.dataframe(policies[policies["policy_id"] == report["policy_id"]], hide_index=True)

st.subheader("Linked readings")
st.caption(
    "From `excursion_report_readings` (the trace links), looked up in `raw/temperature_readings`."
)
linked = views.report_readings(
    report_id,
    session.table(TableName.EXCURSION_REPORT_READINGS),
    session.readings((report["logger_id"],)),
)
st.dataframe(linked, hide_index=True)
