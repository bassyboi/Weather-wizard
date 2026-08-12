import hashlib
import json
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest

from ordcast.daily_baseline import (
    SILO_CUTOFF_UTC_HOUR,
    build_silo_daily_analog_library,
    daily_feature_vector,
)
from ordcast.fusion import EvidenceWeightedScenarioEngine
from pipelines.baseline import silo_daily_analog
from pipelines.ingest import silo_daily
from pipelines.ingest.silo_daily import (
    DailyRainSeries,
    build_silo_station_url,
    parse_silo_rain_csv,
    silo_snapshot_manifest_path,
    write_silo_snapshot,
)


def test_silo_csv_parser_and_url_contract():
    text = """station,date,daily_rain,daily_rain_source
2056,2026-01-01,0.0,0
2056,2026-01-02,12.5,24
"""
    series = parse_silo_rain_csv(text, expected_station=2056)
    assert series.station == 2056
    assert series.rain_mm.tolist() == [0.0, 12.5]
    assert series.source_codes.tolist() == [0, 24]

    url = build_silo_station_url(
        2056, "2026-01-01", "2026-01-02", "farmer@example.com"
    )
    query = parse_qs(urlparse(url).query)
    assert query["station"] == ["2056"]
    assert query["format"] == ["csv"]
    assert query["comment"] == ["R"]


def test_silo_parser_rejects_gaps_and_negative_rain():
    with pytest.raises(ValueError, match="continuous"):
        parse_silo_rain_csv(
            "station,date,daily_rain,daily_rain_source\n"
            "2056,2026-01-01,0,0\n2056,2026-01-03,1,0\n"
        )
    with pytest.raises(ValueError, match="non-negative"):
        parse_silo_rain_csv(
            "station,date,daily_rain,daily_rain_source\n2056,2026-01-01,-1,0\n"
        )


def test_daily_analog_baseline_uses_only_matured_history():
    dates = np.arange(
        np.datetime64("2020-01-01"), np.datetime64("2021-02-01"), np.timedelta64(1, "D")
    )
    rain = np.where(np.arange(len(dates)) % 9 == 0, 12.0, 0.0)
    series = DailyRainSeries(2056, dates, rain, np.zeros(len(dates), dtype=int))
    issue = series.dates[-1] + np.timedelta64(1, "D")
    issue_at = issue.astype("datetime64[ns]") + np.timedelta64(
        SILO_CUTOFF_UTC_HOUR, "h"
    )
    library = build_silo_daily_analog_library(series)
    source, selection = library.forecast(
        daily_feature_vector(series, issue), issue_at, k=20
    )
    forecast = EvidenceWeightedScenarioEngine().fuse([source])
    assert np.all(selection.outcome_available_at <= issue_at)
    assert np.all(np.diff(source.members_mm, axis=1) >= 0)
    assert np.all(np.diff(forecast.quantiles([0.1, 0.5, 0.9]), axis=1) >= 0)


def test_silo_targets_start_after_the_conservatively_skipped_issue_row():
    dates = np.arange(
        np.datetime64("2026-01-01"),
        np.datetime64("2026-02-20"),
        np.timedelta64(1, "D"),
    )
    rain = np.arange(1, len(dates) + 1, dtype=float)
    series = DailyRainSeries(2056, dates, rain, np.zeros(len(dates), dtype=int))
    library = build_silo_daily_analog_library(series, publication_lag_days=0)

    first_issue_index = 30
    assert library.issue_times[0] == (
        dates[first_issue_index].astype("datetime64[ns]")
        + np.timedelta64(SILO_CUTOFF_UTC_HOUR, "h")
    )
    expected = np.cumsum(rain[first_issue_index + 1 : first_issue_index + 8])
    assert library.outcomes_mm[0].tolist() == [
        expected[0],
        expected[2],
        expected[6],
    ]


def test_daily_features_end_on_day_before_issue():
    dates = np.arange(
        np.datetime64("2026-01-01"),
        np.datetime64("2026-03-01"),
        np.timedelta64(1, "D"),
    )
    rain = np.arange(1, len(dates) + 1, dtype=float)
    series = DailyRainSeries(2056, dates, rain, np.zeros(len(dates), dtype=int))
    issue_index = 40

    features = daily_feature_vector(series, dates[issue_index])

    assert features["antecedent_rain_7d"] == pytest.approx(
        rain[issue_index - 7 : issue_index].sum()
    )
    assert features["antecedent_rain_30d"] == pytest.approx(
        rain[issue_index - 30 : issue_index].sum()
    )


def test_default_next_day_issue_can_use_final_snapshot_row():
    dates = np.arange(
        np.datetime64("2026-01-01"),
        np.datetime64("2026-03-01"),
        np.timedelta64(1, "D"),
    )
    rain = np.ones(len(dates))
    series = DailyRainSeries(2056, dates, rain, np.zeros(len(dates), dtype=int))

    features = daily_feature_vector(series, dates[-1] + np.timedelta64(1, "D"))

    assert features["antecedent_rain_30d"] == pytest.approx(30.0)


def test_silo_baseline_rejects_other_station():
    dates = np.arange(
        np.datetime64("2026-01-01"),
        np.datetime64("2026-03-01"),
        np.timedelta64(1, "D"),
    )
    series = DailyRainSeries(
        1234, dates, np.zeros(len(dates)), np.zeros(len(dates), dtype=int)
    )

    with pytest.raises(ValueError, match="Kununurra Aero"):
        build_silo_daily_analog_library(series)
    with pytest.raises(ValueError, match="Kununurra Aero"):
        daily_feature_vector(series, dates[-1] + np.timedelta64(1, "D"))


def test_snapshot_writer_creates_hash_manifest_and_never_overwrites(tmp_path):
    payload = (
        b"station,date,daily_rain,daily_rain_source\n"
        b"2056,2026-01-01,0.0,0\n"
        b"2056,2026-01-02,12.5,24\n"
    )
    snapshot = tmp_path / "kununurra.csv"
    retrieved_at = datetime(2026, 1, 3, 4, 5, tzinfo=timezone.utc)

    manifest_path = write_silo_snapshot(
        payload,
        snapshot,
        station=2056,
        start="2026-01-01",
        finish="2026-01-02",
        retrieved_at=retrieved_at,
    )

    assert snapshot.read_bytes() == payload
    assert manifest_path == silo_snapshot_manifest_path(snapshot)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["retrieved_at_utc"] == "2026-01-03T04:05:00Z"
    assert manifest["sha256"] == hashlib.sha256(payload).hexdigest()
    assert manifest["request"]["station"] == 2056
    assert manifest["response"]["row_count"] == 2
    with pytest.raises(FileExistsError):
        write_silo_snapshot(
            payload,
            snapshot,
            station=2056,
            start="2026-01-01",
            finish="2026-01-02",
        )


def test_snapshot_writer_rejects_incomplete_inclusive_range(tmp_path):
    payload = (
        b"station,date,daily_rain,daily_rain_source\n"
        b"2056,2026-01-01,0.0,0\n"
        b"2056,2026-01-02,12.5,24\n"
    )

    with pytest.raises(ValueError, match="inclusive date range"):
        write_silo_snapshot(
            payload,
            tmp_path / "incomplete.csv",
            station=2056,
            start="2026-01-01",
            finish="2026-01-03",
        )


def test_silo_cli_rejects_nonpositive_analog_count(monkeypatch, tmp_path, capsys):
    missing_csv = tmp_path / "not-read.csv"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "silo_daily_analog",
            "--csv",
            str(missing_csv),
            "--analogs",
            "0",
        ],
    )

    with pytest.raises(SystemExit):
        silo_daily_analog.main()
    assert "--analogs must be positive" in capsys.readouterr().err


def test_silo_cli_rejects_historical_issue_without_snapshot_vintage(
    monkeypatch, tmp_path, capsys
):
    dates = np.arange(
        np.datetime64("2026-01-01"),
        np.datetime64("2026-03-01"),
        np.timedelta64(1, "D"),
    )
    csv_path = tmp_path / "snapshot.csv"
    rows = ["station,date,daily_rain,daily_rain_source"]
    rows.extend(f"2056,{value},0.0,0" for value in dates)
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "silo_daily_analog",
            "--csv",
            str(csv_path),
            "--issue-date",
            "2026-02-01",
        ],
    )

    with pytest.raises(SystemExit):
        silo_daily_analog.main()
    assert "archived snapshot vintage" in capsys.readouterr().err


def test_downloader_rejects_incomplete_requested_range(monkeypatch):
    payload = (
        b"station,date,daily_rain,daily_rain_source\n"
        b"2056,2026-01-01,0.0,0\n"
        b"2056,2026-01-02,12.5,24\n"
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _limit):
            return payload

    monkeypatch.setattr(silo_daily, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    with pytest.raises(ValueError, match="inclusive date range"):
        silo_daily.download_silo_station_daily_rain(
            2056,
            "2026-01-01",
            "2026-01-03",
            "farmer@example.com",
        )
