"""YottaFlex cold-chain viewer: a read-only streamlit app over ``output/<scale>/`` (Phase 9).

Run from the project root (needs the ``dashboard`` extra):

    streamlit run app/streamlit_app.py

It only reads the files ``coldchain run-all`` wrote. It never writes to ``output/`` and never
runs the pipeline. There is no login. Set ``COLDCHAIN_OUTPUT_DIR`` to read another output root.
"""

import streamlit as st

from coldchain.viewer import session

st.set_page_config(page_title="YottaFlex cold-chain viewer", page_icon="❄️", layout="wide")

PAGES = [
    st.Page("views/overview.py", title="Overview", icon=":material/dashboard:", default=True),
    st.Page("views/shipment_explorer.py", title="Shipment Explorer", icon=":material/search:"),
    st.Page("views/temperature_chart.py", title="Temperature Chart", icon=":material/thermostat:"),
    st.Page("views/excursion_reports.py", title="Excursion Reports", icon=":material/warning:"),
    st.Page("views/custody_gaps.py", title="Custody & Data Gaps", icon=":material/link_off:"),
    st.Page("views/data_quality.py", title="Data Quality", icon=":material/verified:"),
]

navigation = st.navigation(PAGES)
selected_scale = session.scale_selector()
st.sidebar.caption(
    f"Reading `{session.output_root() / selected_scale}`. Read-only: this viewer never writes "
    "files and never runs the pipeline."
)
if session.output_ready(selected_scale):
    navigation.run()
