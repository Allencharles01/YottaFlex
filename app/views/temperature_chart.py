"""Temperature Chart: one shipment's readings against its applied policy."""

import streamlit as st

from coldchain.enums import TableName
from coldchain.viewer import charts, session, views

shipments = session.table(TableName.SHIPMENTS)
decisions = session.table(TableName.COMPLIANCE_DECISIONS)
policies = session.table(TableName.THRESHOLD_POLICIES)

st.title("Temperature Chart")
shipment_id = session.pick_shipment(
    sorted(decisions["shipment_id"]), key="chart_shipment", label="Shipment (type to search)"
)
if shipment_id is None:
    st.stop()

decision = views.one_row(decisions, "shipment_id", shipment_id)
policy = views.one_row(policies, "policy_id", decision["applied_policy_id"])
window = views.assignment_window(
    shipment_id,
    shipments,
    session.table(TableName.LOGGERS),
    session.table(TableName.DEVICES),
    session.interval_min(),
)
logger_ids = (window["logger_id"],) if window["logger_id"] else ()
readings = views.classify_readings(session.readings(logger_ids), window)
trusted = readings[readings["reading_class"] == views.ReadingClass.TRUSTED.value]
gaps = views.gap_spans(
    trusted["recorded_at"],
    window["window_start"],
    window["window_end"],
    window["interval_min"],
    int(policy["max_reading_gap_min"]),
)
reports = session.table(TableName.EXCURSION_REPORTS)
excursions = views.excursion_spans(
    reports[reports["shipment_id"] == shipment_id], window["interval_min"]
)

columns = st.columns(5)
columns[0].metric("Engine status", decision["status"])
columns[1].metric("Applied policy", decision["applied_policy_id"])
columns[2].metric("Readings (trusted / all)", f"{len(trusted)} / {len(readings)}")
columns[3].metric("Max gap", f"{decision['max_gap_min']} min")
columns[4].metric("Excursion reports", len(excursions))
st.caption(
    f"Reason codes: {', '.join(views.split_codes(decision['reason_codes'])) or 'none'}. "
    f"Logger `{window['logger_id']}` on device `{window['device_id']}`, every "
    f"{window['interval_min']} min. Policy allows {policy['min_c']:g} to {policy['max_c']:g} °C, "
    f"absolute {policy['abs_min_c']:g} to {policy['abs_max_c']:g} °C, single excursion ≤ "
    f"{policy['max_single_excursion_min']} min, gap ≤ {policy['max_reading_gap_min']} min."
)

if readings.empty:
    st.warning("This shipment has no readings; the whole logger window is a gap.")
st.plotly_chart(
    charts.temperature_chart(readings, policy, excursions, gaps, window), key="temperature_chart"
)

left, right = st.columns(2)
with left:
    st.subheader("Excursion reports")
    if excursions.empty:
        st.caption("The engine found no excursion.")
    else:
        st.dataframe(
            excursions[
                [
                    "report_id",
                    "direction",
                    "started_at",
                    "ended_at",
                    "duration_min",
                    "peak_c",
                    "breach_type",
                ]
            ],
            hide_index=True,
        )
with right:
    st.subheader("Gaps in trusted readings")
    if gaps.empty:
        st.caption(f"No gap: one trusted reading every {window['interval_min']} min.")
    else:
        st.dataframe(gaps, hide_index=True)

with st.expander("All readings"):
    st.dataframe(readings, hide_index=True)
