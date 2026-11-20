"""Custody & Data Gaps: shipments the engine flagged for custody, logger or reading gaps."""

import streamlit as st

from coldchain.enums import TableName
from coldchain.viewer import session, views

decisions = session.table(TableName.COMPLIANCE_DECISIONS)
flagged = views.flagged_shipments(decisions)

st.title("Custody & Data Gaps")
st.caption(
    "Shipments whose engine decision carries CUSTODY_GAP, DATA_GAP, LOW_COVERAGE or "
    "LOGGER_NOT_ASSIGNED."
)
columns = st.columns(len(views.GAP_REASON_CODES))
for column, code in zip(columns, views.GAP_REASON_CODES, strict=True):
    column.metric(code.value, int(flagged[code.value].sum()) if not flagged.empty else 0)

if flagged.empty:
    st.success("No shipment has a custody, logger or data gap.")
    st.stop()
st.dataframe(flagged, hide_index=True)

shipment_id = session.pick_shipment(list(flagged["shipment_id"]), key="gaps_shipment")
shipments = session.table(TableName.SHIPMENTS)
shipment = views.one_row(shipments, "shipment_id", shipment_id)
decision = views.one_row(decisions, "shipment_id", shipment_id)
policy = views.one_row(
    session.table(TableName.THRESHOLD_POLICIES), "policy_id", decision["applied_policy_id"]
)
parties = session.table(TableName.PARTIES)

left, right = st.columns(2)
with left:
    st.subheader("Custody chain")
    chain = views.custody_chain(session.table(TableName.CUSTODY_EVENTS), shipment)
    if chain.empty:
        st.error("No custody events at all.")
    else:
        broken = int((~chain["link_ok"]).sum())
        if broken:
            st.error(f"{broken} broken link(s).")
        else:
            st.success("The custody chain is continuous.")
        st.dataframe(
            views.with_party_names(
                chain[
                    [
                        "sequence",
                        "event_type",
                        "from_party_id",
                        "to_party_id",
                        "event_at",
                        "link_ok",
                        "problem",
                    ]
                ],
                parties,
                ["from_party_id", "to_party_id"],
            ),
            hide_index=True,
        )
with right:
    st.subheader("Logger and reading gaps")
    window = views.assignment_window(
        shipment_id,
        shipments,
        session.table(TableName.LOGGERS),
        session.table(TableName.DEVICES),
        session.interval_min(),
    )
    logger_ids = (window["logger_id"],) if window["logger_id"] else ()
    readings = views.classify_readings(session.readings(logger_ids), window)
    classes = readings["reading_class"].value_counts()
    st.markdown(
        f"Logger `{window['logger_id']}` · assigned device `{window['device_id']}` · coverage "
        f"**{decision['coverage_pct']:g} %** · max gap **{decision['max_gap_min']} min** "
        f"(policy allows {policy['max_reading_gap_min']} min)."
    )
    st.dataframe(classes.rename("readings"))
    trusted = readings[readings["reading_class"] == views.ReadingClass.TRUSTED.value]
    gaps = views.gap_spans(
        trusted["recorded_at"],
        window["window_start"],
        window["window_end"],
        window["interval_min"],
        int(policy["max_reading_gap_min"]),
    )
    if gaps.empty:
        st.caption("No gap in the trusted readings.")
    else:
        st.dataframe(gaps, hide_index=True)
