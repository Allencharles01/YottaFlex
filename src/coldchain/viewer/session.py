"""Streamlit glue shared by every viewer page (needs the ``dashboard`` extra).

* the output root: ``$COLDCHAIN_OUTPUT_DIR`` or the project's ``output/`` folder;
* the sidebar scale selector (scales from ``config/default.yaml``);
* cached, read-only loaders: whole tables and reports are cached per file signature
  (``mtime_ns``, size), so a re-run of the pipeline shows up without a restart; readings are
  read and cached per shipment only;
* the "missing output" message, the "Answer key (ground truth)" label and the shipment picker
  shared between pages.
"""

import os
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from coldchain.config import DEFAULT_OUTPUT_DIR, AppConfig, load_config
from coldchain.enums import ReportKind, TableName
from coldchain.viewer import data

OUTPUT_DIR_ENV = "COLDCHAIN_OUTPUT_DIR"
"""Environment variable that overrides the output root (used by the tests)."""

SCALE_KEY = "scale"
SHIPMENT_KEY = "selected_shipment"
ANSWER_KEY_LABEL = "Answer key (ground truth)"
ANSWER_KEY_NOTE = (
    "From `ground_truth/`: what the generator designed and injected. The compliance engine "
    "never reads it; it is shown only to compare against the engine's output."
)
READINGS_CACHE_ENTRIES = 64


@st.cache_resource(show_spinner=False)
def config() -> AppConfig:
    """Return the validated default config (loaded once per server process)."""
    return load_config()


def output_root() -> Path:
    """Return the output root the viewer reads from."""
    return Path(os.environ.get(OUTPUT_DIR_ENV) or DEFAULT_OUTPUT_DIR)


def run_all_hint(scale: str) -> str:
    """Return the message shown when the output for ``scale`` is missing."""
    return f"Run `coldchain run-all --scale {scale}` first"


def scale_selector() -> str:
    """Draw the sidebar scale selector and return the selected scale."""
    scales = list(config().scales)
    return st.sidebar.selectbox("Scale", scales, key=SCALE_KEY)


def scale() -> str:
    """Return the scale selected in the sidebar (the first config scale before any choice)."""
    return st.session_state.get(SCALE_KEY) or next(iter(config().scales))


def output_ready(selected_scale: str) -> bool:
    """Show the "run the pipeline first" message when required files are missing.

    Returns:
        True when every required file exists.
    """
    missing = data.missing_files(output_root(), selected_scale)
    if not missing:
        return True
    st.info(f"{run_all_hint(selected_scale)}.", icon=":material/info:")
    st.caption(
        f"No complete pipeline output in `{output_root() / selected_scale}` "
        f"({len(missing)} required file(s) missing). This viewer only reads existing files; "
        "it never runs the pipeline."
    )
    with st.expander("Missing files"):
        st.code("\n".join(str(p) for p in missing), language=None)
    return False


@st.cache_data(show_spinner=False)
def _cached_table(root: str, selected_scale: str, table: str, signature: Any) -> pd.DataFrame:
    """Read a table; ``signature`` is only part of the cache key."""
    del signature
    return data.load_table(Path(root), selected_scale, TableName(table))


def table(name: TableName) -> pd.DataFrame:
    """Return a whole table of the selected scale (cached)."""
    root, selected = output_root(), scale()
    signature = data.file_signature(data.table_file(root, selected, name))
    return _cached_table(str(root), selected, name.value, signature)


@st.cache_data(show_spinner=False, max_entries=READINGS_CACHE_ENTRIES)
def _cached_readings(
    root: str, selected_scale: str, logger_ids: tuple[str, ...], signature: Any
) -> pd.DataFrame:
    """Read the readings of some loggers; ``signature`` is only part of the cache key."""
    del signature
    return data.load_readings(Path(root), selected_scale, logger_ids)


def readings(logger_ids: tuple[str, ...]) -> pd.DataFrame:
    """Return only the readings of the given loggers (filtered while reading, cached)."""
    root, selected = output_root(), scale()
    path = data.table_file(root, selected, TableName.TEMPERATURE_READINGS)
    return _cached_readings(
        str(root), selected, tuple(sorted(logger_ids)), data.file_signature(path)
    )


@st.cache_data(show_spinner=False)
def _cached_json(path: str, signature: Any) -> dict[str, Any] | None:
    """Read a JSON document if present; ``signature`` is only part of the cache key."""
    del signature
    return data.load_json_if_present(Path(path))


def json_document(path: Path) -> dict[str, Any] | None:
    """Return a JSON document (cached), or ``None`` if the file does not exist."""
    return _cached_json(str(path), data.file_signature(path))


def optional_document(name: str) -> dict[str, Any] | None:
    """Return an optional document of the selected scale by name, or ``None`` if missing.

    Args:
        name: A key of :func:`coldchain.viewer.data.optional_documents` (``run_summary``,
            ``manifest``, ``baseline_validation``, ``reproducibility``).
    """
    return json_document(data.optional_documents(output_root(), scale())[name])


def _required_report(path: Path) -> dict[str, Any]:
    """Return a required report (its existence is checked by :func:`output_ready`)."""
    document = json_document(path)
    if document is None:  # pragma: no cover - output_ready() guards every page
        raise FileNotFoundError(path)
    return document


def validation_summary() -> dict[str, Any]:
    """Return ``validation/validation_summary.json`` of the selected scale."""
    path = data.report_file(output_root(), scale(), ReportKind.VALIDATION, data.VALIDATION_STEM)
    return _required_report(path)


def evaluation_report() -> dict[str, Any]:
    """Return ``evaluation/evaluation_report.json`` of the selected scale."""
    path = data.report_file(output_root(), scale(), ReportKind.EVALUATION, data.EVALUATION_STEM)
    return _required_report(path)


def answer_key_header(title: str = ANSWER_KEY_LABEL) -> None:
    """Draw the label that marks a section as coming from the answer key."""
    st.subheader(f":material/key: {title}")
    st.warning(ANSWER_KEY_NOTE, icon=":material/key:")


def pick_shipment(shipment_ids: list[str], key: str, label: str = "Shipment") -> str | None:
    """Draw a shipment selectbox that remembers the choice across pages.

    Args:
        shipment_ids: The choices (sorted).
        key: Widget key, unique per page.
        label: Widget label.

    Returns:
        The selected shipment, or ``None`` when there are no choices.
    """
    if not shipment_ids:
        st.info("No shipments to choose from.")
        return None
    remembered = st.session_state.get(SHIPMENT_KEY)
    index = shipment_ids.index(remembered) if remembered in shipment_ids else 0
    choice = st.selectbox(label, shipment_ids, index=index, key=key)
    st.session_state[SHIPMENT_KEY] = choice
    return choice


def open_shipment(shipment_id: str, page: str) -> None:
    """Remember a shipment and switch to another page (e.g. ``views/temperature_chart.py``)."""
    st.session_state[SHIPMENT_KEY] = shipment_id
    st.switch_page(page)


def interval_min() -> int:
    """Return the config's default reading interval (used when a shipment has no logger)."""
    return config().reading_interval_min
