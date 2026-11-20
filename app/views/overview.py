"""Overview page: KPI tiles, status breakdown and the most frequent reason codes."""

import streamlit as st

from coldchain.enums import TableName
from coldchain.viewer import charts, session, views

decisions = session.table(TableName.COMPLIANCE_DECISIONS)
reports = session.table(TableName.EXCURSION_REPORTS)
tiles = views.kpis(decisions, reports)

st.title("Overview")
st.caption(f"Compliance engine output for the `{session.scale()}` scale.")

columns = st.columns(5)
columns[0].metric("Shipments", f"{tiles['shipments']:,}")
columns[1].metric(
    "Compliant",
    f"{tiles['compliant_pct']:g} %",
    help="COMPLIANT + COMPLIANT_WITH_WARNINGS (both lead to RELEASE); "
    f"{tiles['compliant']:,} shipments.",
)
columns[2].metric("Non-compliant", f"{tiles['non_compliant']:,}", help="Quarantined.")
columns[3].metric("Inconclusive", f"{tiles['inconclusive']:,}", help="Sent to review.")
columns[4].metric("Excursion reports", f"{tiles['excursions']:,}")

if decisions.empty:
    st.info("The compliance engine wrote no decisions for this scale.")
    st.stop()

left, right = st.columns(2)
with left:
    st.subheader("Shipments by status")
    counts = views.status_counts(decisions)
    st.plotly_chart(charts.status_bar(counts), key="status_bar")
    st.dataframe(counts, hide_index=True)
with right:
    st.subheader("Shipments by reason code")
    reasons = views.reason_code_counts(decisions)
    if reasons.empty:
        st.info("No reason codes were raised.")
    else:
        st.plotly_chart(charts.reason_bar(reasons), key="reason_bar")

run_summary = session.optional_document("run_summary")
if run_summary is not None:
    st.caption(
        f"Last `run-all`: status **{run_summary.get('status')}**, seed {run_summary.get('seed')}, "
        f"package {run_summary.get('package_version')}, git "
        f"`{str((run_summary.get('git') or {}).get('commit') or '')[:10]}`."
    )
