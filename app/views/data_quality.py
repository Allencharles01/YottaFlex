"""Data Quality: validation summary, evaluation metrics, answer key vs engine, reproducibility."""

import pandas as pd
import streamlit as st

from coldchain.enums import TableName
from coldchain.viewer import session, views

st.title("Data Quality")

# --- Validation -------------------------------------------------------------------------------
summary = session.validation_summary()
totals = summary.get("totals", {})
st.header("Validation summary")
st.caption("`validation/validation_summary.json`: independent integrity and business-rule checks.")
columns = st.columns(5)
columns[0].metric("Status", summary.get("status"))
columns[1].metric("Checks", totals.get("checks"))
columns[2].metric("Passed", totals.get("pass"))
columns[3].metric("Integrity findings", totals.get("integrity_findings"))
columns[4].metric("Business-rule findings", totals.get("business_rule_findings"))
baseline = session.optional_document("baseline_validation")
if baseline is None:
    st.caption("No baseline validation (`coldchain validate --source baseline`) found.")
else:
    st.caption(
        f"Clean baseline snapshot: status **{baseline.get('status')}**, "
        f"{baseline.get('totals', {}).get('findings')} findings."
    )
only_problems = st.toggle("Show only checks that did not pass", value=True, key="dq_problems")
st.dataframe(views.check_table(summary, only_problems), hide_index=True)

# --- Evaluation -------------------------------------------------------------------------------
evaluation = session.evaluation_report()
st.header("Evaluation: engine output scored against the answer key")
excursions = evaluation.get("excursions", {})
columns = st.columns(4)
columns[0].metric("Verdict", evaluation.get("verdict"))
columns[1].metric(
    "Fully correct shipments",
    f"{evaluation.get('fully_correct_shipments')} / {evaluation.get('shipments')}",
)
columns[2].metric("Status accuracy", f"{100 * evaluation.get('status', {}).get('accuracy', 0):g} %")
columns[3].metric(
    "Excursion exact-match rate", f"{100 * (excursions.get('exact_match_rate') or 0):g} %"
)
st.subheader("Detection per injected issue type")
st.dataframe(pd.DataFrame(evaluation.get("issue_types", [])), hide_index=True)
st.subheader("Precision and recall per reason code")
st.dataframe(pd.DataFrame(evaluation.get("reason_codes", [])), hide_index=True)
st.subheader("Status confusion matrix")
st.dataframe(views.confusion_frame(evaluation.get("status", {})))

# --- Answer key vs engine ---------------------------------------------------------------------
session.answer_key_header(f"{session.ANSWER_KEY_LABEL} vs engine")
comparison = views.answer_key_comparison(
    session.table(TableName.EXPECTED_OUTCOMES), session.table(TableName.COMPLIANCE_DECISIONS)
)
mismatches = comparison[~(comparison["status_match"] & comparison["reason_codes_match"])]
st.metric("Shipments where the engine differs from the answer key", len(mismatches))
only_mismatches = st.toggle("Show only differences", value=False, key="dq_mismatches")
st.dataframe(mismatches if only_mismatches else comparison, hide_index=True)
st.subheader(f"Injected anomalies — {session.ANSWER_KEY_LABEL}")
st.dataframe(session.table(TableName.ANOMALIES), hide_index=True)

# --- Reproducibility --------------------------------------------------------------------------
st.header("Reproducibility")
reproducibility = session.optional_document("reproducibility")
if reproducibility is None:
    st.info(
        f"No reproducibility result yet. Run `coldchain reproduce --scale {session.scale()}`.",
        icon=":material/info:",
    )
else:
    result = views.reproducibility_overview(reproducibility)
    columns = st.columns(4)
    columns[0].metric("Verdict", result["verdict"])
    columns[1].metric("Identical files (run1 vs run2)", f"{result['matching']} / {result['files']}")
    columns[2].metric("Changed by the control seed", result["control_changed"])
    columns[3].metric("Seeds", f"{result['seed']} / control {result['control_seed']}")
    st.caption(
        f"dataset_sha256 run1 `{result['dataset_sha256_run1']}` · "
        f"run2 `{result['dataset_sha256_run2']}`"
    )

manifest = session.optional_document("manifest")
if manifest is not None:
    st.caption(
        f"Export manifest: dataset_sha256 `{manifest.get('dataset_sha256')}`, seed "
        f"{manifest.get('seed')}."
    )
