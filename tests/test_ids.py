from datetime import UTC, datetime, timedelta, timezone

import pandas as pd
import pytest

from coldchain import ids
from coldchain.errors import IdFormatError

SHP1 = "SHP-2026-000001"
LOG1 = "LOG-SHP-2026-000001-01"


@pytest.mark.parametrize(
    ("built", "expected", "kind"),
    [
        (ids.party_id(42), "PTY-0042", "party"),
        (ids.party_id(1), "PTY-0001", "party"),
        (ids.product_id(7), "PRD-0007", "product"),
        (ids.policy_id("REFRIG_2_8", 1), "THR-REFRIG_2_8-V1", "policy"),
        (ids.policy_id("AMBIENT_15_25", 2), "THR-AMBIENT_15_25-V2", "policy"),
        (ids.packaging_id(3), "PKG-0003", "packaging"),
        (ids.lot_id(123), "LOT-2026-000123", "lot"),
        (ids.lot_id(1), "LOT-2026-000001", "lot"),
        (ids.device_id(981), "DEV-000981", "device"),
        (ids.shipment_id(4999), "SHP-2026-004999", "shipment"),
        (ids.shipment_id(1), SHP1, "shipment"),
        (ids.logger_id(SHP1), LOG1, "logger"),
        (ids.route_stop_id(SHP1, 2), "RST-SHP-2026-000001-02", "route_stop"),
        (ids.custody_event_id(SHP1, 3), "CUS-SHP-2026-000001-03", "custody_event"),
        (ids.status_event_id(SHP1, 4), "STE-SHP-2026-000001-04", "status_event"),
        (ids.excursion_report_id(57), "EXCR-2026-000057", "excursion_report"),
        (ids.anomaly_id(14), "ANOM-000014", "anomaly"),
        (
            ids.reading_id(LOG1, datetime(2026, 3, 10, 9, 10, tzinfo=UTC)),
            "TEMP-LOG-SHP-2026-000001-01-20260310T0910",
            "reading",
        ),
    ],
)
def test_id_formats_match_plan(built: str, expected: str, kind: str) -> None:
    assert built == expected
    assert ids.ID_PATTERNS[kind].match(built)


def test_every_pattern_is_covered() -> None:
    assert set(ids.ID_PATTERNS) == {
        "party",
        "product",
        "policy",
        "packaging",
        "lot",
        "device",
        "shipment",
        "logger",
        "reading",
        "route_stop",
        "custody_event",
        "status_event",
        "excursion_report",
        "anomaly",
    }


def test_max_width_boundaries() -> None:
    assert ids.party_id(9999) == "PTY-9999"
    assert ids.shipment_id(999999) == "SHP-2026-999999"
    assert ids.route_stop_id(SHP1, 99) == "RST-SHP-2026-000001-99"
    with pytest.raises(IdFormatError, match="outside"):
        ids.party_id(10000)
    with pytest.raises(IdFormatError, match="outside"):
        ids.shipment_id(1_000_000)
    with pytest.raises(IdFormatError, match="outside"):
        ids.route_stop_id(SHP1, 100)


@pytest.mark.parametrize("bad", [0, -1, 1.0, "1", True, None])
def test_invalid_numbers_rejected(bad: object) -> None:
    with pytest.raises(IdFormatError):
        ids.device_id(bad)  # type: ignore[arg-type]


def test_custom_year_and_invalid_year() -> None:
    assert ids.shipment_id(5, year=2027) == "SHP-2027-000005"
    with pytest.raises(IdFormatError, match="4-digit"):
        ids.lot_id(1, year=26)


def test_policy_id_rejects_bad_code_and_version() -> None:
    with pytest.raises(IdFormatError):
        ids.policy_id("refrig-2-8", 1)
    with pytest.raises(IdFormatError):
        ids.policy_id("REFRIG_2_8", 0)


def test_child_ids_reject_malformed_shipment() -> None:
    for builder in (ids.route_stop_id, ids.custody_event_id, ids.status_event_id):
        with pytest.raises(IdFormatError, match="shipment"):
            builder("SHP-26-1", 1)
    with pytest.raises(IdFormatError, match="shipment"):
        ids.logger_id("SHP-2026-1")


def test_reading_id_converts_to_utc_and_rejects_naive() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    local = datetime(2026, 3, 10, 14, 40, tzinfo=ist)  # == 09:10 UTC
    assert ids.reading_id(LOG1, local) == "TEMP-LOG-SHP-2026-000001-01-20260310T0910"
    with pytest.raises(IdFormatError, match="timezone-aware"):
        ids.reading_id(LOG1, datetime(2026, 3, 10, 9, 10))
    with pytest.raises(IdFormatError, match="logger"):
        ids.reading_id("LOG-1", datetime(2026, 3, 10, 9, 10, tzinfo=UTC))


def test_vectorised_reading_ids_match_scalar() -> None:
    times = pd.Series(pd.date_range("2026-03-10 09:00", periods=4, freq="10min", tz="UTC"))
    loggers = pd.Series([LOG1, LOG1, "LOG-SHP-2026-000002-01", LOG1])
    result = ids.reading_ids(loggers, times)
    expected = [ids.reading_id(lg, t.to_pydatetime()) for lg, t in zip(loggers, times, strict=True)]
    assert result.tolist() == expected
    assert result.tolist()[1] == "TEMP-LOG-SHP-2026-000001-01-20260310T0910"


def test_vectorised_reading_ids_errors() -> None:
    naive = pd.Series(pd.date_range("2026-03-10", periods=2, freq="10min"))
    with pytest.raises(IdFormatError, match="timezone-aware"):
        ids.reading_ids(pd.Series([LOG1, LOG1]), naive)
    with pytest.raises(IdFormatError, match="Length mismatch"):
        ids.reading_ids(pd.Series([LOG1]), naive)


def test_vectorised_reading_ids_empty() -> None:
    empty_times = pd.Series(pd.DatetimeIndex([], tz="UTC"))
    assert ids.reading_ids(pd.Series([], dtype=str), empty_times).empty


# ---------------------------------------------------------------- vectorised scoped IDs


@pytest.mark.parametrize(
    ("vectorised", "scalar"),
    [
        (ids.route_stop_ids, ids.route_stop_id),
        (ids.custody_event_ids, ids.custody_event_id),
        (ids.status_event_ids, ids.status_event_id),
    ],
)
def test_vectorised_scoped_ids_match_scalar(vectorised, scalar) -> None:
    shipments = pd.Series([SHP1, SHP1, "SHP-2026-004999"])
    sequences = pd.Series([1, 2, 99])
    expected = [scalar(s, n) for s, n in zip(shipments, sequences, strict=True)]
    assert vectorised(shipments, sequences).tolist() == expected


def test_vectorised_logger_ids_match_scalar() -> None:
    shipments = pd.Series([SHP1, "SHP-2026-000002"])
    assert ids.logger_ids(shipments).tolist() == [ids.logger_id(s) for s in shipments]
    assert ids.logger_ids(shipments, number=2).tolist()[0] == "LOG-SHP-2026-000001-02"


@pytest.mark.parametrize("sequences", [[0], [100], [1.5]])
def test_vectorised_scoped_ids_reject_bad_sequences(sequences) -> None:
    with pytest.raises(IdFormatError):
        ids.route_stop_ids(pd.Series([SHP1]), pd.Series(sequences))


def test_vectorised_scoped_ids_reject_nulls_and_length_mismatch() -> None:
    with pytest.raises(IdFormatError):
        ids.route_stop_ids(pd.Series([SHP1]), pd.Series([1, None], dtype="Int64"))
    with pytest.raises(IdFormatError, match="Length mismatch"):
        ids.route_stop_ids(pd.Series([SHP1]), pd.Series([1, 2]))


def test_vectorised_scoped_ids_handle_empty_input() -> None:
    empty = ids.custody_event_ids(pd.Series([], dtype=str), pd.Series([], dtype="int64"))
    assert empty.empty


def test_embedded_shipment_ids() -> None:
    values = pd.Series(
        [
            LOG1,
            "TEMP-LOG-SHP-2026-000007-01-20260310T0910",
            "RST-SHP-2026-000003-02",
            "CUS-SHP-2026-000004-03",
            "STE-SHP-2026-000005-04",
            "DEV-000001",
        ]
    )
    result = ids.embedded_shipment_ids(values)
    assert result.tolist()[:5] == [
        SHP1,
        "SHP-2026-000007",
        "SHP-2026-000003",
        "SHP-2026-000004",
        "SHP-2026-000005",
    ]
    assert pd.isna(result.iloc[5])
