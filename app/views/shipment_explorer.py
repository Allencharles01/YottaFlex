"""Shipment Explorer: one shipment's decision, lots, packaging, device, route, custody, history."""

import streamlit as st

from coldchain.enums import TableName
from coldchain.viewer import session, views

TEMPERATURE_PAGE = "views/temperature_chart.py"

shipments = session.table(TableName.SHIPMENTS)
decisions = session.table(TableName.COMPLIANCE_DECISIONS)
parties = session.table(TableName.PARTIES)

st.title("Shipment Explorer")
status_filter = st.multiselect(
    "Filter by engine status",
    sorted(decisions["status"].unique()),
    key="explorer_status",
    placeholder="All statuses",
)
candidates = decisions
if status_filter:
    candidates = decisions[decisions["status"].isin(status_filter)]
shipment_id = session.pick_shipment(
    sorted(candidates["shipment_id"]), key="explorer_shipment", label="Shipment (type to search)"
)
if shipment_id is None:
    st.stop()

shipment = views.one_row(shipments, "shipment_id", shipment_id)
decision = views.one_row(decisions, "shipment_id", shipment_id)

st.header(shipment_id)
if decision is not None:
    columns = st.columns(4)
    columns[0].metric("Engine status", decision["status"])
    columns[1].metric("Action", decision["decided_action"])
    columns[2].metric("Coverage", f"{decision['coverage_pct']:g} %")
    columns[3].metric("Total excursion", f"{decision['total_excursion_min']} min")
    st.markdown(
        f"**Applied policy** `{decision['applied_policy_id']}` ({decision['policy_source']}) · "
        f"**Reason codes** {', '.join(views.split_codes(decision['reason_codes'])) or 'none'} · "
        f"**Excursion reports** "
        f"{', '.join(views.split_codes(decision['excursion_report_ids'])) or 'none'}"
    )
if st.button("Open temperature chart", icon=":material/thermostat:", key="explorer_chart"):
    session.open_shipment(shipment_id, TEMPERATURE_PAGE)

st.subheader("Shipment")
party_columns = ["supplier_party_id", "carrier_party_id", "receiver_party_id", "planner_party_id"]
named = views.with_party_names(
    shipments[shipments["shipment_id"] == shipment_id], parties, party_columns
)
st.dataframe(views.field_table(named.iloc[0]), hide_index=True)

st.subheader("Lots and products")
st.dataframe(
    views.shipment_lots_detail(
        shipment_id,
        session.table(TableName.SHIPMENT_LOTS),
        session.table(TableName.LOTS),
        session.table(TableName.PRODUCTS),
    ),
    hide_index=True,
)

left, right = st.columns(2)
with left:
    st.subheader("Packaging")
    packaging = session.table(TableName.PACKAGING)
    st.dataframe(
        views.field_table(views.one_row(packaging, "packaging_id", shipment["packaging_id"])),
        hide_index=True,
    )
with right:
    st.subheader("Logger, device and calibration")
    window = views.assignment_window(
        shipment_id,
        shipments,
        session.table(TableName.LOGGERS),
        session.table(TableName.DEVICES),
        session.interval_min(),
    )
    if not window["has_logger"]:
        st.warning("No logger assignment: the window falls back to the actual departure/arrival.")
    st.dataframe(views.field_table(window), hide_index=True)

st.subheader("Route stops")
stops = session.table(TableName.ROUTE_STOPS)
st.dataframe(
    views.with_party_names(
        stops[stops["shipment_id"] == shipment_id].sort_values("sequence"), parties, ["party_id"]
    ),
    hide_index=True,
)

st.subheader("Custody timeline")
chain = views.custody_chain(session.table(TableName.CUSTODY_EVENTS), shipment)
if chain.empty:
    st.warning("No custody events recorded for this shipment.")
else:
    if not chain["link_ok"].all():
        st.error("The custody chain is broken; see the `problem` column.")
    st.dataframe(
        views.with_party_names(chain, parties, ["from_party_id", "to_party_id"]), hide_index=True
    )

st.subheader("Status history")
st.caption("Raw lifecycle from the generator, then the events appended by the compliance engine.")
st.dataframe(
    views.with_party_names(
        views.status_history(
            session.table(TableName.SHIPMENT_STATUS_EVENTS),
            session.table(TableName.COMPLIANCE_STATUS_EVENTS),
            shipment_id,
        ),
        parties,
        ["actor_party_id"],
    ),
    hide_index=True,
)

with st.expander(f"{session.ANSWER_KEY_LABEL} for {shipment_id}"):
    session.answer_key_header()
    expected = session.table(TableName.EXPECTED_OUTCOMES)
    st.dataframe(
        views.field_table(views.one_row(expected, "shipment_id", shipment_id), "answer key"),
        hide_index=True,
    )
    anomalies = session.table(TableName.ANOMALIES)
    injected = anomalies[anomalies["shipment_id"] == shipment_id]
    if injected.empty:
        st.caption("No anomaly was injected into this shipment.")
    else:
        st.dataframe(injected, hide_index=True)
