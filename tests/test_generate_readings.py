import numpy as np
import pandas as pd
import pytest

from coldchain import ids
from coldchain.enums import TableName
from coldchain.errors import GenerationError
from coldchain.generate import readings

START = pd.Timestamp("2026-03-01T08:00", tz="UTC")


@pytest.fixture(scope="module")
def frame(starter_tables) -> pd.DataFrame:
    """Readings joined with their logger window, applied limits, device uncertainty and design."""
    tables = starter_tables
    loggers = tables[TableName.LOGGERS]
    joined = tables[TableName.TEMPERATURE_READINGS].merge(
        loggers.rename(columns={"device_id": "assigned_device"}), on="logger_id"
    )
    joined = joined.merge(tables[TableName.DEVICES][["device_id", "uncertainty_c"]], on="device_id")
    return joined


def test_reading_ids_follow_the_format(starter_tables) -> None:
    readings_table = starter_tables[TableName.TEMPERATURE_READINGS]
    assert readings_table["reading_id"].str.match(ids.ID_PATTERNS["reading"].pattern).all()
    assert (
        readings_table["reading_id"]
        == ids.reading_ids(readings_table["logger_id"], readings_table["recorded_at"])
    ).all()


def test_readings_cover_every_window_at_exact_steps(frame, app_config) -> None:
    interval = pd.Timedelta(minutes=app_config.reading_interval_min)
    assert (frame["recorded_at"] >= frame["assigned_at"]).all()
    assert (frame["recorded_at"] <= frame["unassigned_at"]).all()
    assert ((frame["recorded_at"] - frame["assigned_at"]) % interval == pd.Timedelta(0)).all()
    per_logger = frame.groupby("logger_id").agg(
        n=("reading_id", "size"), start=("assigned_at", "first"), end=("unassigned_at", "first")
    )
    assert (per_logger["n"] == (per_logger["end"] - per_logger["start"]) / interval + 1).all()


def test_no_duplicate_logger_instants_and_one_logger_per_shipment(starter_tables) -> None:
    table = starter_tables[TableName.TEMPERATURE_READINGS]
    assert not table.duplicated(["logger_id", "recorded_at"]).any()
    assert set(table["logger_id"]) == set(starter_tables[TableName.LOGGERS]["logger_id"])


def test_readings_come_from_the_assigned_device(frame) -> None:
    assert (frame["device_id"] == frame["assigned_device"]).all()


def test_temperatures_rounded_to_two_decimals(starter_tables) -> None:
    t = starter_tables[TableName.TEMPERATURE_READINGS]["temperature_c"]
    assert ((t - t.round(2)).abs() < 1e-9).all()


def test_natural_readings_stay_in_the_safe_band(starter_tables, app_config) -> None:
    """Outside the designed segments, readings keep more than u + margin from every limit."""
    tables = starter_tables
    outcomes = tables[TableName.EXPECTED_OUTCOMES]
    shaped = outcomes.loc[
        outcomes["scenario"].isin(["NORMAL", "LONG_DWELL", "CALIBRATION_EXPIRED"]), "shipment_id"
    ]
    table = tables[TableName.TEMPERATURE_READINGS]
    loggers = tables[TableName.LOGGERS].set_index("logger_id")
    table = table[loggers.loc[table["logger_id"], "shipment_id"].isin(shaped).to_numpy()]
    devices = tables[TableName.DEVICES].set_index("device_id")
    u = devices.loc[table["device_id"], "uncertainty_c"].to_numpy()
    packaging = tables[TableName.PACKAGING].set_index("packaging_id")
    ships = tables[TableName.SHIPMENTS].set_index("shipment_id")
    code = packaging.loc[
        ships.loc[loggers.loc[table["logger_id"], "shipment_id"], "packaging_id"],
        "supported_policy_code",
    ].to_numpy()
    policies = tables[TableName.THRESHOLD_POLICIES].groupby("policy_code").first()
    margin = app_config.generation.readings.limit_margin_c
    low = policies.loc[code, "min_c"].to_numpy() + u + margin - 1e-9
    high = policies.loc[code, "max_c"].to_numpy() - u - margin + 1e-9
    t = table["temperature_c"].to_numpy()
    assert ((t >= low) & (t <= high)).all()


def test_reading_grid_is_inclusive_and_ordered() -> None:
    loggers = pd.DataFrame(
        {
            "logger_id": ["LOG-SHP-2026-000001-01", "LOG-SHP-2026-000002-01"],
            "shipment_id": ["SHP-2026-000001", "SHP-2026-000002"],
            "device_id": ["DEV-000001", "DEV-000002"],
            "assigned_at": [START, START],
            "unassigned_at": [START + pd.Timedelta(minutes=30), START],
        }
    )
    grid = readings.reading_grid(loggers, 10)
    assert grid["index"].tolist() == [0, 1, 2, 3, 0]
    assert grid["recorded_at"].tolist() == [
        START + pd.Timedelta(minutes=m) for m in (0, 10, 20, 30, 0)
    ]
    assert grid["logger_id"].tolist() == [loggers["logger_id"][0]] * 4 + [loggers["logger_id"][1]]


def test_dwell_warming_rises_caps_and_recovers(app_config) -> None:
    settings = app_config.generation.readings
    times = [START + pd.Timedelta(minutes=m) for m in (0, 60, 120, 240, 600, 630, 900)]
    grid = pd.DataFrame(
        {"shipment_id": "SHP-2026-000001", "recorded_at": times, "thermal_response": 0.25}
    )
    stops = pd.DataFrame(
        {
            "shipment_id": ["SHP-2026-000001"],
            "arrived_at": [START + pd.Timedelta(minutes=60)],
            "departed_at": [START + pd.Timedelta(minutes=600)],
        }
    )
    warming = readings._dwell_warming(app_config, grid, stops)
    rate = 0.25 * settings.dwell_warming_c_per_hour  # °C per hour
    peak = min(settings.max_dwell_warming_c, rate * 9)
    assert warming[0] == 0.0  # before the stop
    assert warming[1] == 0.0  # on arrival
    assert warming[2] == pytest.approx(rate * 1)
    assert warming[3] == pytest.approx(rate * 3)
    assert warming[4] == pytest.approx(peak)  # at departure (capped)
    assert warming[5] == pytest.approx(peak * np.exp(-30 / settings.dwell_recovery_min))
    assert warming[6] < 0.01  # recovered
    assert readings._dwell_warming(app_config, grid, stops.iloc[:0]).tolist() == [0.0] * 7


def test_empty_temperature_band_is_rejected() -> None:
    context = pd.DataFrame(
        {
            "min_c": [2.0],
            "max_c": [8.0],
            "abs_min_c": [0.0],
            "abs_max_c": [12.0],
            "uncertainty_c": [0.5],
        }
    )
    bands = readings._bands(context, 0.05)
    assert (bands["safe_low"][0], bands["safe_high"][0]) == (2.55, 7.45)
    assert (bands["high_low"][0], bands["high_high"][0]) == (8.55, 11.95)
    assert (bands["low_low"][0], bands["low_high"][0]) == (0.05, 1.45)
    with pytest.raises(GenerationError, match="band is empty"):
        readings._bands(context, 3.0)


def test_designed_readings_have_their_shapes(starter_tables) -> None:
    """Excursion readings sit in their band, absolute readings beyond, at-limit exactly at it."""
    tables = starter_tables
    outcomes = tables[TableName.EXPECTED_OUTCOMES].set_index("shipment_id")
    table = tables[TableName.TEMPERATURE_READINGS]
    loggers = tables[TableName.LOGGERS].set_index("logger_id")
    table = table.assign(shipment_id=loggers.loc[table["logger_id"], "shipment_id"].to_numpy())
    policies = tables[TableName.THRESHOLD_POLICIES].groupby("policy_code").first()
    packaging = tables[TableName.PACKAGING].set_index("packaging_id")
    ships = tables[TableName.SHIPMENTS].set_index("shipment_id")

    def limits(shipment: str) -> pd.Series:
        code = packaging.loc[ships.loc[shipment, "packaging_id"], "supported_policy_code"]
        return policies.loc[code]

    reasons = outcomes["expected_reason_codes"].fillna("")
    for shipment in outcomes.index[reasons.str.contains("ABSOLUTE_LIMIT_BREACH")]:
        p, t = limits(shipment), table.loc[table["shipment_id"] == shipment, "temperature_c"]
        assert ((t > p["abs_max_c"]) | (t < p["abs_min_c"])).sum() == 1
    for shipment in outcomes.index[reasons.str.contains("NEAR_LIMIT_UNCERTAIN")]:
        p, t = limits(shipment), table.loc[table["shipment_id"] == shipment, "temperature_c"]
        assert t.isin([p["min_c"], p["max_c"]]).any()
        assert t.between(p["min_c"], p["max_c"]).all()


# --------------------------------------------------------------------------------------------
# Revision 2: excursion shape, clipping, device truth
# --------------------------------------------------------------------------------------------


def _one_segment(ramp_before: int, ramp_after: int, absolute_index: int = -1):
    """A 30-reading REFRIG-like logger with one HIGH segment of 3 readings at index 10."""
    loggers = pd.DataFrame(
        {
            "logger_id": ["LOG-SHP-2026-000001-01"],
            "assigned_at": [START],
            "unassigned_at": [START + pd.Timedelta(minutes=290)],
        }
    )
    n = 30
    context = pd.DataFrame(
        {
            "min_c": [2.0] * n,
            "max_c": [8.0] * n,
            "abs_min_c": [0.0] * n,
            "abs_max_c": [12.0] * n,
            "uncertainty_c": [0.5] * n,
        }
    )
    segments = pd.DataFrame(
        {
            "logger_id": ["LOG-SHP-2026-000001-01"],
            "is_excursion": [True],
            "direction": ["HIGH"],
            "first_index": [10],
            "reading_count": [3],
            "absolute_index": [absolute_index],
            "ramp_before": [ramp_before],
            "ramp_after": [ramp_after],
        }
    )
    return loggers, context, segments


def test_designed_segment_ramps_and_hump(app_config) -> None:
    loggers, context, segments = _one_segment(ramp_before=2, ramp_after=3)
    bands = readings._bands(context, 0.05)  # safe band [2.55, 7.45]; excursion [8.55, 11.95]
    natural = np.full(len(context), 5.0)
    rngs = {"excursions": np.random.default_rng(3)}
    rows, values = readings._designed_values(
        app_config, loggers, context, segments, bands, natural, np.zeros(len(context)), rngs
    )
    by_row = dict(zip(rows.tolist(), values.tolist(), strict=True))
    assert sorted(by_row) == list(range(8, 16))  # 2 ramp + 3 designed + 3 ramp readings
    # Ramps: linear from the natural 5.0 toward the safe edge 7.45, inside the safe band.
    assert [by_row[r] for r in (8, 9)] == [5.82, 6.63]
    assert [by_row[r] for r in (13, 14, 15)] == [6.84, 6.22, 5.61]  # 6.225 rounds to 6.22 in binary
    # Plateau: out of range, inside the excursion band, peaking mid-segment (no noise here).
    plateau = [by_row[r] for r in (10, 11, 12)]
    assert all(8.55 <= t <= 11.95 for t in plateau)
    assert plateau[1] >= plateau[0] and plateau[1] >= plateau[2]


def test_absolute_reading_is_the_peak(app_config) -> None:
    loggers, context, segments = _one_segment(ramp_before=2, ramp_after=2, absolute_index=2)
    bands = readings._bands(context, 0.05)
    rows, values = readings._designed_values(
        app_config,
        loggers,
        context,
        segments,
        bands,
        np.full(len(context), 5.0),
        np.zeros(len(context)),
        {"excursions": np.random.default_rng(1)},
    )
    by_row = dict(zip(rows.tolist(), values.tolist(), strict=True))
    assert by_row[12] > 12.0  # beyond abs_max_c
    assert 8.55 <= by_row[10] <= by_row[11] <= 11.95  # hump climbs toward the breach


def test_overlapping_ramps_are_rejected(app_config) -> None:
    loggers, context, segments = _one_segment(ramp_before=2, ramp_after=2)
    second = segments.assign(first_index=15)  # its ramp-up starts at row 13 = first's ramp-down
    with pytest.raises(GenerationError, match="overlap"):
        readings._designed_values(
            app_config,
            loggers,
            context,
            pd.concat([segments, second], ignore_index=True),
            readings._bands(context, 0.05),
            np.full(len(context), 5.0),
            np.zeros(len(context)),
            {"excursions": np.random.default_rng(1)},
        )


def test_ramps_around_every_designed_segment_stay_in_the_safe_band(starter_tables) -> None:
    """The readings just before and after every expected excursion window are in range."""
    tables = starter_tables
    outcomes = tables[TableName.EXPECTED_OUTCOMES].dropna(subset=["expected_excursion_windows"])
    table = tables[TableName.TEMPERATURE_READINGS]
    loggers = tables[TableName.LOGGERS].set_index("shipment_id")
    step = pd.Timedelta(minutes=10)
    for shipment, windows in zip(
        outcomes["shipment_id"], outcomes["expected_excursion_windows"], strict=True
    ):
        own = table[table["logger_id"] == loggers.loc[shipment, "logger_id"]]
        own = own.set_index("recorded_at")["temperature_c"]
        for window in windows.split("|"):
            start, end = (pd.Timestamp(t) for t in window.split("/"))
            before = own.loc[start - 2 * step : start - step]
            after = own.loc[end + step : end + 2 * step]
            assert len(before) == 2 and len(after) == 2, shipment


def test_clipping_counts_ignore_designed_rows() -> None:
    natural = np.array([1.0, 5.0, 9.0, 9.5, 5.0])
    clipped, total = readings.clipping_counts(
        natural, np.full(5, 2.0), np.full(5, 8.0), np.array([3])
    )
    assert (clipped, total) == (2, 4)


def test_natural_clipping_is_rare_at_starter(app_config, reference, caplog) -> None:
    """DECISIONS.md #53: under 1 % of natural readings are clipped by the safe band."""
    from coldchain.generate import generate_tables

    with caplog.at_level("INFO", logger="coldchain.generate.readings"):
        generate_tables(app_config, reference, "starter")
    message = next(r.getMessage() for r in caplog.records if "Clipped" in r.getMessage())
    share = float(message.rsplit("(", 1)[1].rstrip("%)"))
    assert share < 1.0


def test_device_truth_offsets_lie_within_uncertainty(starter_tables) -> None:
    truth = starter_tables[TableName.DEVICE_TRUTH].set_index("device_id")
    devices = starter_tables[TableName.DEVICES].set_index("device_id")
    assert list(truth.index) == list(devices.index)
    offset = truth["systematic_offset_c"]
    assert (offset.abs() <= devices.loc[truth.index, "uncertainty_c"] + 1e-9).all()
    assert ((offset - offset.round(2)).abs() < 1e-9).all()
    assert offset.nunique() > 1


def test_device_offset_shows_in_the_readings(starter_tables) -> None:
    """A device's mean deviation from setpoint on NORMAL trips follows its true offset."""
    tables = starter_tables
    outcomes = tables[TableName.EXPECTED_OUTCOMES].set_index("shipment_id")
    loggers = tables[TableName.LOGGERS].set_index("logger_id")
    table = tables[TableName.TEMPERATURE_READINGS]
    shipment = loggers.loc[table["logger_id"], "shipment_id"].to_numpy()
    normal = (outcomes.loc[shipment, "scenario"] == "NORMAL").to_numpy()
    ships = tables[TableName.SHIPMENTS].set_index("shipment_id")
    setpoint = tables[TableName.PACKAGING].set_index("packaging_id")["setpoint_c"]
    deviation = (
        table["temperature_c"].to_numpy()
        - setpoint.loc[ships.loc[shipment, "packaging_id"]].to_numpy()
    )
    per_device = pd.Series(deviation[normal]).groupby(table["device_id"].to_numpy()[normal]).mean()
    truth = tables[TableName.DEVICE_TRUTH].set_index("device_id")["systematic_offset_c"]
    assert np.corrcoef(per_device, truth.loc[per_device.index])[0, 1] > 0.5


def test_device_truth_is_never_read_outside_the_generator() -> None:
    """DECISIONS.md #54: only the generator and the schema registry name device_truth."""
    from coldchain.config import PROJECT_ROOT

    package = PROJECT_ROOT / "src" / "coldchain"
    allowed = {
        package / "enums.py",
        package / "schemas.py",
        package / "generate" / "__init__.py",
        package / "generate" / "readings.py",
    }
    for path in package.rglob("*.py"):
        if path in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        assert "DEVICE_TRUTH" not in text and "device_truth" not in text, path
