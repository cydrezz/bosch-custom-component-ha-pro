"""Tests for Recording Sensor fallback logic and _insert_statistics."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import logging
import sys
import os
import zoneinfo

import pytest

# Ensure custom_components is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.bosch.sensor.recording import RecordingSensor

TZ_BERLIN = zoneinfo.ZoneInfo("Europe/Berlin")


def _make_sensor(*, new_stats_api=False):
    """Create a minimally mocked RecordingSensor for unit testing."""
    sensor = object.__new__(RecordingSensor)
    # Minimal attributes the methods expect
    sensor._bosch_object = MagicMock()
    sensor._attr_uri = "/recording/test"
    sensor._update_init = False
    sensor._new_stats_api = new_stats_api
    sensor._short_id = "testsensorid"
    sensor._name = "Test Sensor"
    sensor._unit_of_measurement = "kWh"
    sensor._attr_device_class = "energy"
    sensor._attr_state_class = "total"
    sensor._attr_last_reset = None
    sensor._state = None
    sensor._domain_name = "Recording"
    sensor.hass = MagicMock()
    sensor.entity_id = "sensor.test_recording"
    sensor.async_schedule_update_ha_state = MagicMock()
    sensor.async_write_ha_state = MagicMock()
    return sensor


# ---------------------------------------------------------------------------
# Tests for async_old_gather_update
# ---------------------------------------------------------------------------


class TestAsyncOldGatherUpdate:
    """Tests for the old (HTTP/IVT) gather update path."""

    @pytest.mark.asyncio
    async def test_exact_hour_match(self):
        """When data contains exact last-hour entry, use its value directly."""
        sensor = _make_sensor()
        now = datetime(2026, 3, 1, 14, 6, 0, tzinfo=TZ_BERLIN)
        last_hour = datetime(2026, 3, 1, 13, 0, 0, tzinfo=TZ_BERLIN)

        sensor._bosch_object.get_property.return_value = {
            "value": [
                {"d": last_hour, "value": 7.5},
            ]
        }
        sensor._bosch_object.unit_of_measurement = "kWh"
        sensor._bosch_object.device_class = "energy"
        sensor._bosch_object.state_class = "total"

        with patch("custom_components.bosch.sensor.recording.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            await sensor.async_old_gather_update()

        assert sensor._state == 7.5

    @pytest.mark.asyncio
    async def test_fallback_within_6h(self):
        """When last-hour is missing but latest data is <6h old, use fallback."""
        sensor = _make_sensor()
        # Current time: 18:06, looking for 17:00, but gateway only has up to 15:00
        now = datetime(2026, 3, 1, 18, 6, 0, tzinfo=TZ_BERLIN)
        available_hour = datetime(2026, 3, 1, 15, 0, 0, tzinfo=TZ_BERLIN)

        sensor._bosch_object.get_property.return_value = {
            "value": [
                {"d": datetime(2026, 3, 1, 13, 0, 0, tzinfo=TZ_BERLIN), "value": 2.0},
                {"d": datetime(2026, 3, 1, 14, 0, 0, tzinfo=TZ_BERLIN), "value": 3.0},
                {"d": available_hour, "value": 4.2},
            ]
        }
        sensor._bosch_object.unit_of_measurement = "kWh"
        sensor._bosch_object.device_class = "energy"
        sensor._bosch_object.state_class = "total"

        with patch("custom_components.bosch.sensor.recording.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            await sensor.async_old_gather_update()

        # Should use the latest available value (4.2) as fallback
        assert sensor._state == 4.2

    @pytest.mark.asyncio
    async def test_stale_data_returns_none(self):
        """When latest data is >6h old, state is None (unknown)."""
        sensor = _make_sensor()
        # Current time: 22:06, but gateway only has data until 14:00 (8h lag)
        now = datetime(2026, 3, 1, 22, 6, 0, tzinfo=TZ_BERLIN)
        old_hour = datetime(2026, 3, 1, 14, 0, 0, tzinfo=TZ_BERLIN)

        sensor._bosch_object.get_property.return_value = {
            "value": [
                {"d": old_hour, "value": 1.5},
            ]
        }
        sensor._bosch_object.unit_of_measurement = "kWh"
        sensor._bosch_object.device_class = "energy"
        sensor._bosch_object.state_class = "total"

        with patch("custom_components.bosch.sensor.recording.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            await sensor.async_old_gather_update()

        assert sensor._state is None

    @pytest.mark.asyncio
    async def test_no_data_returns_early(self):
        """When no data available, _state should not change."""
        sensor = _make_sensor()
        sensor._state = "previous_value"

        sensor._bosch_object.get_property.return_value = None

        with patch("custom_components.bosch.sensor.recording.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 1, 14, 6, 0, tzinfo=TZ_BERLIN)
            await sensor.async_old_gather_update()

        # Should return early without changing state
        assert sensor._state == "previous_value"

    @pytest.mark.asyncio
    async def test_empty_value_list_returns_early(self):
        """When data has empty value list, _state should not change."""
        sensor = _make_sensor()
        sensor._state = "previous_value"

        sensor._bosch_object.get_property.return_value = {"value": []}

        with patch("custom_components.bosch.sensor.recording.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 1, 14, 6, 0, tzinfo=TZ_BERLIN)
            await sensor.async_old_gather_update()

        # Empty list is falsy, so should return early
        assert sensor._state == "previous_value"

    @pytest.mark.asyncio
    async def test_fallback_boundary_exactly_6h(self):
        """Fallback at exactly 6h should still be accepted (<=)."""
        sensor = _make_sensor()
        now = datetime(2026, 3, 1, 20, 6, 0, tzinfo=TZ_BERLIN)
        # Data from exactly 6 hours ago
        boundary_hour = datetime(2026, 3, 1, 14, 6, 0, tzinfo=TZ_BERLIN)

        sensor._bosch_object.get_property.return_value = {
            "value": [
                {"d": boundary_hour, "value": 9.9},
            ]
        }
        sensor._bosch_object.unit_of_measurement = "kWh"
        sensor._bosch_object.device_class = "energy"
        sensor._bosch_object.state_class = "total"

        with patch("custom_components.bosch.sensor.recording.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            await sensor.async_old_gather_update()

        assert sensor._state == 9.9

    @pytest.mark.asyncio
    async def test_fallback_just_over_6h_returns_none(self):
        """Fallback at 6h + 1 second should be rejected, state is None."""
        sensor = _make_sensor()
        now = datetime(2026, 3, 1, 20, 6, 1, tzinfo=TZ_BERLIN)
        # Data from exactly 6h + 1s ago
        old_hour = datetime(2026, 3, 1, 14, 6, 0, tzinfo=TZ_BERLIN)

        sensor._bosch_object.get_property.return_value = {
            "value": [
                {"d": old_hour, "value": 9.9},
            ]
        }
        sensor._bosch_object.unit_of_measurement = "kWh"
        sensor._bosch_object.device_class = "energy"
        sensor._bosch_object.state_class = "total"

        with patch("custom_components.bosch.sensor.recording.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            await sensor.async_old_gather_update()

        assert sensor._state is None

    @pytest.mark.asyncio
    async def test_stale_data_logs_warning(self, caplog):
        """Stale data should produce a warning log about clock sync."""
        sensor = _make_sensor()
        sensor._attr_unique_id = "test_unique"
        now = datetime(2026, 3, 1, 22, 6, 0, tzinfo=TZ_BERLIN)
        old_hour = datetime(2026, 3, 1, 14, 0, 0, tzinfo=TZ_BERLIN)

        sensor._bosch_object.get_property.return_value = {
            "value": [
                {"d": old_hour, "value": 1.5},
            ]
        }
        sensor._bosch_object.unit_of_measurement = "kWh"
        sensor._bosch_object.device_class = "energy"
        sensor._bosch_object.state_class = "total"

        with (
            patch("custom_components.bosch.sensor.recording.dt_util") as mock_dt,
            caplog.at_level(logging.WARNING),
        ):
            mock_dt.now.return_value = now
            await sensor.async_old_gather_update()

        assert "too old" in caplog.text or "clock synchronization" in caplog.text.lower()


# ---------------------------------------------------------------------------
# Tests for _insert_statistics else-branch
# ---------------------------------------------------------------------------


class TestInsertStatisticsElseBranch:
    """Tests for the _insert_statistics method's else-branch handling."""

    @pytest.mark.asyncio
    async def test_else_branch_falls_back_to_last_stat(self):
        """When statistic_id not in last_stats, should fall back to last_stat."""
        import asyncio

        sensor = _make_sensor(new_stats_api=True)
        sensor._statistic_import_lock = asyncio.Lock()

        stat_id = sensor.statistic_id
        now = datetime(2026, 3, 1, 14, 6, 0, tzinfo=TZ_BERLIN)
        # Use a recent timestamp so the code takes the simple path (not fetch_past_data)
        recent_ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=TZ_BERLIN).timestamp()

        # last_stat contains our statistic_id (so we pass the entry check)
        mock_last_stat = {
            stat_id: [{"state": 5.0, "sum": 100.0, "start": recent_ts}]
        }
        # last_stats from get_stats_from_ha_db returns a DIFFERENT key (triggers else)
        mock_last_stats_ha = {
            "other:statistic_id": [{"state": 3.0, "sum": 50.0, "start": recent_ts}]
        }

        sensor.get_last_stat = AsyncMock(return_value=mock_last_stat)
        sensor.get_stats_from_ha_db = AsyncMock(return_value=mock_last_stats_ha)

        # Track calls to append_statistics
        sensor.append_statistics = MagicMock(return_value=100.0)
        sensor._bosch_object.state = [
            {"d": datetime(2026, 3, 1, 10, 0, 0, tzinfo=TZ_BERLIN), "value": 2.0}
        ]
        sensor.get_last_stats_before_date = MagicMock(
            return_value={"start": recent_ts, "sum": 100.0}
        )
        # statistics are up to date -> plain hourly run

        with patch("custom_components.bosch.sensor.recording.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.start_of_local_day.return_value = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            await sensor._insert_statistics()

        # append_statistics should have been called (via else branch using last_stat)
        sensor.append_statistics.assert_called_once()

    @pytest.mark.asyncio
    async def test_normal_path_when_statistic_id_present(self):
        """When statistic_id IS in last_stats, normal path should work."""
        import asyncio

        sensor = _make_sensor(new_stats_api=True)
        sensor._statistic_import_lock = asyncio.Lock()

        stat_id = sensor.statistic_id
        now = datetime(2026, 3, 1, 14, 6, 0, tzinfo=TZ_BERLIN)
        recent_ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=TZ_BERLIN).timestamp()

        mock_last_stat = {
            stat_id: [{"state": 5.0, "sum": 100.0, "start": recent_ts}]
        }
        # last_stats ALSO contains our statistic_id (normal path)
        mock_last_stats_ha = {
            stat_id: [{"state": 5.0, "sum": 100.0, "start": recent_ts}]
        }

        sensor.get_last_stat = AsyncMock(return_value=mock_last_stat)
        sensor.get_stats_from_ha_db = AsyncMock(return_value=mock_last_stats_ha)
        sensor.append_statistics = MagicMock(return_value=100.0)
        sensor._bosch_object.state = [
            {"d": datetime(2026, 3, 1, 10, 0, 0, tzinfo=TZ_BERLIN), "value": 2.0}
        ]
        sensor.get_last_stats_before_date = MagicMock(
            return_value={"start": recent_ts, "sum": 100.0}
        )
        # statistics are up to date -> plain hourly run

        with patch("custom_components.bosch.sensor.recording.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.start_of_local_day.return_value = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            await sensor._insert_statistics()

        sensor.append_statistics.assert_called_once()
        # base row start (float ts from the recorder) arrives as datetime
        fill_from = sensor.append_statistics.call_args.kwargs["fill_from"]
        assert fill_from == datetime(2026, 3, 1, 10, 0, 0, tzinfo=TZ_BERLIN)

    @pytest.mark.asyncio
    async def test_initial_fetch_when_no_stats_exist(self):
        """When no stats exist at all, should fetch 30 days of data."""
        import asyncio

        sensor = _make_sensor(new_stats_api=True)
        sensor._statistic_import_lock = asyncio.Lock()

        now = datetime(2026, 3, 1, 14, 6, 0, tzinfo=TZ_BERLIN)

        # Empty last_stat → triggers initial fetch
        sensor.get_last_stat = AsyncMock(return_value={})

        mock_past_data = {
            "row1": {"d": datetime(2026, 2, 28, 10, 0, 0, tzinfo=TZ_BERLIN), "value": 3.0},
            "row2": {"d": datetime(2026, 2, 28, 11, 0, 0, tzinfo=TZ_BERLIN), "value": 4.0},
        }
        sensor.fetch_past_data = AsyncMock(return_value=mock_past_data)
        sensor.append_statistics = MagicMock(return_value=7.0)

        with patch("custom_components.bosch.sensor.recording.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            await sensor._insert_statistics()

        sensor.fetch_past_data.assert_called_once()
        sensor.append_statistics.assert_called_once()


# ---------------------------------------------------------------------------
# Baseline row after a day without statistic rows (0 kWh day = no rows)
# ---------------------------------------------------------------------------


class TestBaselineAfterGapDay:
    """The baseline must be the newest row BEFORE the day, never a row of it."""

    @staticmethod
    def _row(dt, state, _sum):
        return {"start": dt.timestamp(), "state": state, "sum": _sum}

    @pytest.mark.asyncio
    async def test_found_in_window_needs_no_extra_query(self):
        sensor = _make_sensor(new_stats_api=True)
        day = datetime(2026, 9, 9, 0, 0, 0, tzinfo=TZ_BERLIN)
        before = self._row(day - timedelta(hours=1), 1.0, 100.0)
        today = self._row(day + timedelta(hours=7), 1.0, 101.0)
        sensor.get_stats_from_ha_db = AsyncMock()

        row = await sensor.async_get_last_stats_before_date(
            {sensor.statistic_id: [before, today]}, day
        )

        assert row is before
        sensor.get_stats_from_ha_db.assert_not_called()

    @pytest.mark.asyncio
    async def test_gap_day_searches_further_back(self):
        """Window holds only rows of the day itself -> query older rows."""
        sensor = _make_sensor(new_stats_api=True)
        day = datetime(2026, 9, 9, 0, 0, 0, tzinfo=TZ_BERLIN)
        today = self._row(day + timedelta(hours=7), 1.0, 109.0)
        older = self._row(day - timedelta(days=2, hours=3), 0.5, 108.0)
        sensor.get_stats_from_ha_db = AsyncMock(
            return_value={sensor.statistic_id: [older]}
        )

        row = await sensor.async_get_last_stats_before_date(
            {sensor.statistic_id: [today]}, day
        )

        assert row is older  # old code returned `today` -> phantom kWh
        kwargs = sensor.get_stats_from_ha_db.call_args.kwargs
        assert kwargs["end_time"] == day
        assert kwargs["start_time"] == day - timedelta(days=30)

    @pytest.mark.asyncio
    async def test_no_row_before_day_starts_from_zero(self):
        sensor = _make_sensor(new_stats_api=True)
        day = datetime(2026, 9, 9, 0, 0, 0, tzinfo=TZ_BERLIN)
        today = self._row(day + timedelta(hours=7), 1.0, 1.0)
        sensor.get_stats_from_ha_db = AsyncMock(return_value={})

        row = await sensor.async_get_last_stats_before_date(
            {sensor.statistic_id: [today]}, day
        )

        assert row is None
        assert sensor.get_stats_from_ha_db.call_count == 2  # 30 d, then all

    @pytest.mark.asyncio
    async def test_insert_statistics_uses_row_before_gap_day(self):
        """Hourly run on the day after a 0 kWh day keeps the old sum as base."""
        import asyncio

        sensor = _make_sensor(new_stats_api=True)
        sensor._statistic_import_lock = asyncio.Lock()
        stat_id = sensor.statistic_id
        now = datetime(2026, 9, 9, 14, 6, 0, tzinfo=TZ_BERLIN)
        start_of_day = now.replace(hour=0, minute=0)
        today = self._row(start_of_day + timedelta(hours=7), 1.0, 109.0)
        before_gap = self._row(start_of_day - timedelta(hours=27), 0.5, 108.0)

        async def stats_from_db(start_time, end_time):
            rows = [
                r
                for r in (before_gap, today)
                if start_time.timestamp() <= r["start"] < end_time.timestamp()
            ]
            return {stat_id: rows} if rows else {}

        sensor.get_last_stat = AsyncMock(return_value={stat_id: [today]})
        sensor.get_stats_from_ha_db = AsyncMock(side_effect=stats_from_db)
        sensor.append_statistics = MagicMock(return_value=109.0)
        sensor._bosch_object.state = [
            {"d": start_of_day + timedelta(hours=7), "value": 1.0}
        ]
        # base row is older than yesterday -> catch-up fetch from the gateway
        sensor.fetch_past_data = AsyncMock(
            return_value={0: {"d": start_of_day + timedelta(hours=7), "value": 1.0}}
        )

        with patch("custom_components.bosch.sensor.recording.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.start_of_local_day.side_effect = lambda d=None: (
                (d or now).replace(hour=0, minute=0, second=0, microsecond=0)
            )
            await sensor._insert_statistics()

        # old code took the newest row (109.0, a row of today) as base
        sensor.append_statistics.assert_called_once()
        assert sensor.append_statistics.call_args.kwargs["sum"] == 108.0
        # fill starts after the base row -> the whole gap day becomes 0 rows
        fill_from = sensor.append_statistics.call_args.kwargs["fill_from"]
        assert fill_from == start_of_day - timedelta(hours=27)


# ---------------------------------------------------------------------------
# Tests for append_statistics (0 kWh hours become rows, 0.28.8.7)
# ---------------------------------------------------------------------------


class TestAppendStatisticsFillsQuietHours:
    """The client library drops 0 kWh hours; append_statistics fills them.

    Without a 23:00 row the statistic card (period start - 1 h) takes the
    first row of the day as baseline and loses that hour.
    """

    @staticmethod
    def _pushed(sensor):
        return sensor.add_external_stats.call_args.kwargs["stats"]

    def test_fills_hours_between_baseline_and_first_row(self):
        sensor = _make_sensor(new_stats_api=True)
        sensor.add_external_stats = MagicMock()
        day = datetime(2026, 10, 31, 0, 0, tzinfo=TZ_BERLIN)
        base = day - timedelta(hours=3)  # 30.10. 21:00 = last row in DB
        rows = [
            {"d": day, "value": 2.0},
            {"d": day + timedelta(hours=1), "value": 1.0},
        ]

        total = sensor.append_statistics(
            stats=rows, sum=9789.6, now=day, fill_from=base
        )

        pushed = self._pushed(sensor)
        assert [p["start"] for p in pushed] == [
            day - timedelta(hours=2),  # 22:00 filled
            day - timedelta(hours=1),  # 23:00 filled
            day,
            day + timedelta(hours=1),
        ]
        assert [p["state"] for p in pushed] == [0, 0, 2.0, 1.0]
        assert [round(p["sum"], 1) for p in pushed] == [
            9789.6,
            9789.6,
            9791.6,
            9792.6,
        ]
        assert total == pytest.approx(9792.6)

    def test_fills_gap_between_data_rows_but_not_after_last(self):
        sensor = _make_sensor(new_stats_api=True)
        sensor.add_external_stats = MagicMock()
        day = datetime(2026, 10, 31, 0, 0, tzinfo=TZ_BERLIN)
        rows = [
            {"d": day + timedelta(hours=2), "value": 1.0},
            {"d": day + timedelta(hours=5), "value": 2.0},
        ]

        sensor.append_statistics(stats=rows, sum=10.0, now=day)

        pushed = self._pushed(sensor)
        assert [p["start"].hour for p in pushed] == [2, 3, 4, 5]
        assert [p["state"] for p in pushed] == [1.0, 0, 0, 2.0]
        assert pushed[-1]["sum"] == pytest.approx(13.0)
        # nothing after the newest row: later hours may be the future

    def test_without_baseline_starts_at_first_row(self):
        sensor = _make_sensor(new_stats_api=True)
        sensor.add_external_stats = MagicMock()
        day = datetime(2026, 10, 31, 0, 0, tzinfo=TZ_BERLIN)
        rows = [{"d": day + timedelta(hours=7), "value": 3.0}]

        sensor.append_statistics(stats=rows, sum=0, now=day)

        pushed = self._pushed(sensor)
        assert len(pushed) == 1
        assert pushed[0]["start"] == day + timedelta(hours=7)

    def test_zero_and_duplicate_rows_are_ignored(self):
        sensor = _make_sensor(new_stats_api=True)
        sensor.add_external_stats = MagicMock()
        day = datetime(2026, 10, 31, 0, 0, tzinfo=TZ_BERLIN)
        rows = [
            {"d": day + timedelta(hours=1), "value": 0},
            {"d": day + timedelta(hours=2), "value": 1.0},
            {"d": day + timedelta(hours=2), "value": 1.0},
        ]

        total = sensor.append_statistics(stats=rows, sum=5.0, now=day)

        pushed = self._pushed(sensor)
        assert [p["start"].hour for p in pushed] == [2]
        assert total == pytest.approx(6.0)

    def test_utc_baseline_from_recorder_mixes_with_local_rows(self):
        """Recorder rows carry UTC timestamps, gateway rows local time."""
        sensor = _make_sensor(new_stats_api=True)
        sensor.add_external_stats = MagicMock()
        day = datetime(2026, 10, 31, 0, 0, tzinfo=TZ_BERLIN)
        base_utc = datetime(2026, 10, 30, 20, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        rows = [{"d": day, "value": 2.0}]

        sensor.append_statistics(stats=rows, sum=1.0, now=day, fill_from=base_utc)

        pushed = self._pushed(sensor)
        # 30.10. 21:00 UTC = 22:00 local, 22:00 UTC = 23:00 local, then 00:00
        assert len(pushed) == 3
        assert pushed[0]["start"] == day - timedelta(hours=2)
        assert pushed[1]["start"] == day - timedelta(hours=1)
        assert pushed[2]["start"] == day


# ---------------------------------------------------------------------------
# _upsert_past_statistics: compare the full date, not the day of month
# ---------------------------------------------------------------------------


class TestUpsertPastStatisticsDateCheck:
    """`now.day == start.day` also refused the same day of another month."""

    @staticmethod
    async def _upsert(start):
        sensor = _make_sensor(new_stats_api=True)
        sensor.fetch_past_data = AsyncMock(return_value={})
        now = datetime(2026, 11, 14, 10, 0, 0, tzinfo=TZ_BERLIN)
        with patch("custom_components.bosch.sensor.recording.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            await sensor._upsert_past_statistics(
                start=start, stop=start + timedelta(hours=24)
            )
        return sensor

    @pytest.mark.asyncio
    async def test_same_day_number_in_other_month_is_fetched(self):
        sensor = await self._upsert(datetime(2026, 10, 14, 0, 0, 0, tzinfo=TZ_BERLIN))
        sensor.fetch_past_data.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_today_is_still_refused(self):
        sensor = await self._upsert(datetime(2026, 11, 14, 0, 0, 0, tzinfo=TZ_BERLIN))
        sensor.fetch_past_data.assert_not_awaited()


# ---------------------------------------------------------------------------
# fetch_past_data: a range fetch must not hang forever
# ---------------------------------------------------------------------------


class TestFetchPastDataTimeout:
    """bosch-thermostat-client 0.28.2 loops forever on an empty day answer."""

    @pytest.mark.asyncio
    async def test_endless_library_loop_is_cut_off(self):
        sensor = _make_sensor(new_stats_api=True)

        async def fetch_range(start_time, stop_time):
            while True:  # `if not data: continue` without advancing the date
                await asyncio.sleep(0)

        sensor._bosch_object.fetch_range = fetch_range
        day = datetime(2026, 11, 13, 0, 0, 0, tzinfo=TZ_BERLIN)

        with patch(
            "custom_components.bosch.sensor.statistic_helper"
            ".FETCH_RANGE_TIMEOUT_PER_DAY",
            0.05,
        ):
            task = asyncio.ensure_future(
                sensor.fetch_past_data(
                    start_time=day, stop_time=day + timedelta(hours=26)
                )
            )
            done, _ = await asyncio.wait({task}, timeout=5)
        if not done:  # old code: never returns
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert done, "fetch_past_data is still running after 5 s"
        with pytest.raises(TimeoutError):
            task.result()

    @pytest.mark.asyncio
    async def test_answer_in_time_is_passed_through(self):
        sensor = _make_sensor(new_stats_api=True)
        day = datetime(2026, 11, 13, 0, 0, 0, tzinfo=TZ_BERLIN)
        data = {day: {"d": day, "value": 1.0}}
        sensor._bosch_object.fetch_range = AsyncMock(return_value=data)

        result = await sensor.fetch_past_data(
            start_time=day, stop_time=day + timedelta(hours=26)
        )

        assert result is data


# ---------------------------------------------------------------------------
# Yesterday is re-fetched after a missed midnight run (0.28.8.9)
# ---------------------------------------------------------------------------


@pytest.fixture
def berlin_time_zone():
    """fetch_past_data takes the local midnight from the HA time zone."""
    from homeassistant.util import dt as ha_dt

    old = ha_dt.DEFAULT_TIME_ZONE
    ha_dt.set_default_time_zone(TZ_BERLIN)
    yield
    ha_dt.set_default_time_zone(old)


@pytest.mark.usefixtures("berlin_time_zone")
class TestCloseYesterdayAfterMissedMidnight:
    """The library fetches yesterday only in the run right after midnight.

    A missed run left the last hours of yesterday as 0 kWh rows for good.
    Re-fetching them must never stop the hourly write.
    """

    DAY = datetime(2026, 11, 14, 0, 0, 0, tzinfo=TZ_BERLIN)
    YESTERDAY = datetime(2026, 11, 13, 0, 0, 0, tzinfo=TZ_BERLIN)

    @staticmethod
    def _row(dt, state, _sum):
        return {"start": dt.timestamp(), "state": state, "sum": _sum}

    @staticmethod
    def _gw(*rows):
        """Answer of fetch_range: {date: {"d": date, "value": kWh}}."""
        return {d: {"d": d, "value": value} for d, value in rows}

    def _sensor(self, db_rows, state):
        sensor = _make_sensor(new_stats_api=True)
        sensor._statistic_import_lock = asyncio.Lock()
        stat_id = sensor.statistic_id
        sensor.db_rows = db_rows

        async def stats_from_db(start_time, end_time):
            rows = [
                r
                for r in sensor.db_rows
                if start_time.timestamp() <= r["start"] < end_time.timestamp()
            ]
            return {stat_id: rows} if rows else {}

        async def last_stat():
            return {stat_id: [max(sensor.db_rows, key=lambda r: r["start"])]}

        sensor.get_last_stat = AsyncMock(side_effect=last_stat)
        sensor.get_stats_from_ha_db = AsyncMock(side_effect=stats_from_db)
        sensor.add_external_stats = MagicMock()
        sensor._bosch_object.state = state
        return sensor

    async def _run(self, sensor, now):
        """One hourly run; returns the written rows as {start: row}."""
        sensor.add_external_stats.reset_mock()
        with patch("custom_components.bosch.sensor.recording.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.start_of_local_day.side_effect = lambda d=None: (
                (d or now).replace(hour=0, minute=0, second=0, microsecond=0)
            )
            await sensor.async_update()
        if not sensor.add_external_stats.called:
            return {}
        return {
            row["start"]: row
            for row in sensor.add_external_stats.call_args.kwargs["stats"]
        }

    def _down_over_midnight(self):
        """HA was down from 19:30 until after midnight."""
        y = self.YESTERDAY
        db_rows = [
            self._row(y - timedelta(hours=1), 1.0, 100.0),
            self._row(y + timedelta(hours=18), 2.0, 102.0),
            self._row(y + timedelta(hours=19), 1.0, 103.0),  # partial hour
        ]
        state = [
            {"d": self.DAY, "value": 0.5},
            {"d": self.DAY + timedelta(hours=1), "value": 0.2},
        ]
        return self._sensor(db_rows, state)

    def _gateway_yesterday(self):
        y = self.YESTERDAY
        return self._gw(
            # left over in the library from an older call: must be ignored
            (y - timedelta(hours=3), 9.0),
            (y + timedelta(hours=18), 2.0),
            (y + timedelta(hours=19), 1.5),
            (y + timedelta(hours=21), 3.0),
            (y + timedelta(hours=23), 1.0),
            (self.DAY + timedelta(hours=1), 7.0),  # today comes from state
        )

    @pytest.mark.asyncio
    async def test_missed_midnight_run_gets_the_late_hours(self):
        sensor = self._down_over_midnight()
        sensor._bosch_object.fetch_range = AsyncMock(
            return_value=self._gateway_yesterday()
        )
        y = self.YESTERDAY

        written = await self._run(sensor, self.DAY.replace(hour=1, minute=6))

        # one request, for yesterday only
        sensor._bosch_object.fetch_range.assert_called_once_with(
            start_time=y, stop_time=self.DAY
        )
        assert sensor._day_closed == self.DAY
        # old code: 20:00-23:00 of yesterday as 0 kWh, 4.5 kWh lost for good
        assert written[y + timedelta(hours=19)]["state"] == 1.5
        assert written[y + timedelta(hours=20)]["state"] == 0
        assert written[y + timedelta(hours=21)]["state"] == 3.0
        assert written[y + timedelta(hours=23)]["state"] == 1.0
        assert written[y + timedelta(hours=23)]["sum"] == pytest.approx(107.5)
        last = written[self.DAY + timedelta(hours=1)]
        assert last["state"] == 0.2
        assert last["sum"] == pytest.approx(108.2)
        # rebuilt from the row before yesterday, every hour exactly once
        assert min(written) == y
        assert len(written) == 26

    @pytest.mark.asyncio
    async def test_yesterday_is_fetched_only_once_per_day(self):
        sensor = self._down_over_midnight()
        sensor._bosch_object.fetch_range = AsyncMock(
            return_value=self._gateway_yesterday()
        )

        await self._run(sensor, self.DAY.replace(hour=1, minute=6))
        written = await self._run(sensor, self.DAY.replace(hour=2, minute=6))

        assert sensor._bosch_object.fetch_range.call_count == 1
        assert written  # the normal path still writes

    @pytest.mark.asyncio
    async def test_restart_fetches_again_and_keeps_the_sums(self):
        """A new object (HA restart) re-fetches; same data -> same sums."""
        y = self.YESTERDAY
        db_rows = [
            self._row(y - timedelta(hours=1), 1.0, 100.0),
            self._row(y + timedelta(hours=22), 2.0, 102.0),
            self._row(y + timedelta(hours=23), 1.0, 103.0),
            self._row(self.DAY + timedelta(hours=14), 0.5, 103.5),
        ]
        state = [{"d": self.DAY + timedelta(hours=14), "value": 0.5}]
        sensor = self._sensor(db_rows, state)
        sensor._bosch_object.fetch_range = AsyncMock(
            return_value=self._gw(
                (y + timedelta(hours=22), 2.0), (y + timedelta(hours=23), 1.0)
            )
        )

        written = await self._run(sensor, self.DAY.replace(hour=15, minute=6))

        sensor._bosch_object.fetch_range.assert_called_once()
        for row in db_rows[1:]:
            start = datetime.fromtimestamp(row["start"], TZ_BERLIN)
            assert written[start]["state"] == row["state"]
            assert written[start]["sum"] == pytest.approx(row["sum"])

    @pytest.mark.asyncio
    async def test_partial_last_hour_that_ended_at_zero_becomes_zero(self):
        """Same as the run right after midnight: left out = 0 kWh."""
        sensor = self._down_over_midnight()
        y = self.YESTERDAY
        # 19:00 was written as a partial value (1.0), the gateway leaves it out
        sensor._bosch_object.fetch_range = AsyncMock(
            return_value=self._gw(
                (y + timedelta(hours=18), 2.0), (y + timedelta(hours=23), 1.0)
            )
        )

        written = await self._run(sensor, self.DAY.replace(hour=1, minute=6))

        assert written[y + timedelta(hours=18)]["state"] == 2.0
        assert written[y + timedelta(hours=19)]["state"] == 0
        assert written[y + timedelta(hours=23)]["sum"] == pytest.approx(103.0)

    @pytest.mark.asyncio
    async def test_answer_with_less_than_the_recorder_is_not_used(self, caplog):
        """The re-fetch may add to yesterday, never take away."""
        sensor = self._down_over_midnight()
        y = self.YESTERDAY
        # 18:00 is in the recorder with 2.0 kWh, the gateway leaves it out
        sensor._bosch_object.fetch_range = AsyncMock(
            return_value=self._gw((y + timedelta(hours=23), 1.0))
        )

        with caplog.at_level(logging.WARNING):
            written = await self._run(
                sensor, self.DAY.replace(hour=1, minute=6)
            )
            again = await self._run(sensor, self.DAY.replace(hour=2, minute=6))

        # normal path: 0 kWh rows after the last row, then today's rows
        assert min(written) == y + timedelta(hours=20)
        assert written[y + timedelta(hours=23)]["state"] == 0
        assert written[self.DAY]["sum"] == pytest.approx(103.5)
        assert min(again) == y + timedelta(hours=20)
        assert sensor._day_closed == self.DAY
        assert sensor._bosch_object.fetch_range.call_count == 1
        assert "yesterday is left as it is" in caplog.text

    @pytest.mark.asyncio
    async def test_clock_change_day_with_25_entries_falls_back(self, caplog):
        """The library raises ValueError (hour=24) - the hour is still written."""
        sensor = self._down_over_midnight()
        sensor._bosch_object.fetch_range = AsyncMock(
            side_effect=ValueError("hour must be in 0..23")
        )

        with caplog.at_level(logging.WARNING):
            written = await self._run(
                sensor, self.DAY.replace(hour=1, minute=6)
            )

        # normal path: today's rows on top of the last row before them
        assert written[self.DAY]["state"] == 0.5
        assert written[self.DAY]["sum"] == pytest.approx(103.5)
        assert sensor._day_closed is None  # next hour tries again
        assert "Could not re-fetch yesterday" in caplog.text

    @pytest.mark.asyncio
    async def test_empty_day_is_cut_off_and_falls_back(self):
        """Empty answer = the library never returns; the timeout ends it."""
        sensor = self._down_over_midnight()
        calls = []

        async def fetch_range(start_time, stop_time):
            calls.append(start_time)
            while True:  # `if not data: continue` without advancing the date
                await asyncio.sleep(0)

        sensor._bosch_object.fetch_range = fetch_range

        with patch(
            "custom_components.bosch.sensor.recording.CLOSE_DAY_FETCH_TIMEOUT",
            0.05,
        ):
            task = asyncio.ensure_future(
                self._run(sensor, self.DAY.replace(hour=1, minute=6))
            )
            done, _ = await asyncio.wait({task}, timeout=5)
        if not done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert done, "the hourly run is still running after 5 s"
        written = task.result()
        assert written[self.DAY]["sum"] == pytest.approx(103.5)
        # the library floods the gateway until the timeout: once per day only
        assert sensor._day_closed == self.DAY
        written = await self._run(sensor, self.DAY.replace(hour=2, minute=6))
        assert written[self.DAY]["sum"] == pytest.approx(103.5)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_permanent_failure_gives_up_and_keeps_writing(self):
        """Three failed runs, every one writes; then no more requests."""
        from bosch_thermostat_client.exceptions import DeviceException

        sensor = self._down_over_midnight()
        sensor._bosch_object.fetch_range = AsyncMock(
            side_effect=DeviceException("gateway says no")
        )

        for hour in (1, 2, 3, 4, 5):
            written = await self._run(
                sensor, self.DAY.replace(hour=hour, minute=6)
            )
            assert written[self.DAY]["state"] == 0.5, hour

        assert sensor._bosch_object.fetch_range.call_count == 3
        assert sensor._day_closed == self.DAY

        # the next day starts with a fresh set of attempts
        next_day = self.DAY + timedelta(days=1)
        sensor.db_rows.append(
            self._row(self.DAY + timedelta(hours=5), 0.3, 103.8)
        )
        sensor._bosch_object.state = [{"d": next_day, "value": 0.4}]
        await self._run(sensor, next_day.replace(hour=1, minute=6))
        assert sensor._bosch_object.fetch_range.call_count == 4

    @pytest.mark.asyncio
    async def test_without_rows_of_today_yesterday_is_left_alone(self):
        """Rows of today in the recorder but none from the gateway: wait."""
        y = self.YESTERDAY
        db_rows = [
            self._row(y - timedelta(hours=1), 1.0, 100.0),
            self._row(y + timedelta(hours=23), 1.0, 101.0),
            self._row(self.DAY + timedelta(hours=9), 0.5, 101.5),
        ]
        sensor = self._sensor(db_rows, state=[])
        sensor._bosch_object.fetch_range = AsyncMock(return_value={})

        written = await self._run(sensor, self.DAY.replace(hour=10, minute=6))

        sensor._bosch_object.fetch_range.assert_not_called()
        assert written == {}
        assert sensor._day_closed is None

    @pytest.mark.asyncio
    async def test_no_row_before_yesterday_skips_the_fetch(self):
        y = self.YESTERDAY
        db_rows = [self._row(y + timedelta(hours=23), 1.0, 1.0)]
        sensor = self._sensor(db_rows, [{"d": self.DAY, "value": 0.5}])
        sensor._bosch_object.fetch_range = AsyncMock(return_value={})

        written = await self._run(sensor, self.DAY.replace(hour=1, minute=6))

        sensor._bosch_object.fetch_range.assert_not_called()
        assert sensor._day_closed == self.DAY
        assert written[self.DAY]["sum"] == pytest.approx(1.5)

    @pytest.mark.asyncio
    async def test_recorder_failure_aborts_the_run_but_not_for_ever(self):
        """A failed recorder query still ends the cycle without a write."""
        from custom_components.bosch.sensor.statistic_helper import (
            StatisticsQueryError,
        )

        sensor = self._down_over_midnight()
        sensor._bosch_object.fetch_range = AsyncMock(
            return_value=self._gateway_yesterday()
        )
        healthy = sensor.get_stats_from_ha_db.side_effect

        async def broken_inside_close(start_time, end_time):
            if start_time == self.YESTERDAY:  # the query of _close_yesterday
                raise StatisticsQueryError("db locked")
            return await healthy(start_time, end_time)

        sensor.get_stats_from_ha_db = AsyncMock(side_effect=broken_inside_close)

        for hour in (1, 2, 3):
            written = await self._run(
                sensor, self.DAY.replace(hour=hour, minute=6)
            )
            assert written == {}, hour
        sensor._bosch_object.fetch_range.assert_not_called()

        # after CLOSE_DAY_MAX_ATTEMPTS runs the normal path takes over
        written = await self._run(sensor, self.DAY.replace(hour=4, minute=6))
        assert written[self.DAY]["state"] == 0.5

    @pytest.mark.asyncio
    async def test_row_of_today_behind_the_state_waits(self):
        """A row of today newer than the state would keep its old sum."""
        y = self.YESTERDAY
        db_rows = [
            self._row(y - timedelta(hours=1), 1.0, 100.0),
            self._row(y + timedelta(hours=23), 1.0, 101.0),
            self._row(self.DAY + timedelta(hours=9), 0.5, 101.5),
            self._row(self.DAY + timedelta(hours=11), 0.1, 101.6),
        ]
        state = [{"d": self.DAY + timedelta(hours=9), "value": 0.5}]
        sensor = self._sensor(db_rows, state)
        sensor._bosch_object.fetch_range = AsyncMock(return_value={})

        written = await self._run(sensor, self.DAY.replace(hour=12, minute=6))

        sensor._bosch_object.fetch_range.assert_not_called()
        assert sensor._day_closed is None
        # the normal path still writes this hour
        assert written[self.DAY + timedelta(hours=9)]["sum"] == pytest.approx(
            101.5
        )


class _FakeGateway:
    """Answers GET like the gateway: 24 (or 25) hourly entries per day."""

    def __init__(self, days):
        self.days = days  # {"2026-11-13": [kWh per hour, ...]}
        self.requests = []

    async def get(self, uri):
        self.requests.append(uri)
        await asyncio.sleep(0)
        hours = self.days.get(uri.rsplit("=", 1)[1])
        if hours is None:
            return {}
        return {
            "interval": uri.rsplit("=", 1)[1],
            "recording": [
                {"y": round(kwh * 10), "c": 10 if kwh else 0} for kwh in hours
            ],
        }


def _library_object(gateway):
    """A RecordingSensor of bosch-thermostat-client without its __init__."""
    from bosch_thermostat_client.sensors.recording import (
        RecordingSensor as LibraryRecordingSensor,
    )

    lib = object.__new__(LibraryRecordingSensor)
    lib._lock = asyncio.Lock()
    lib._past_data = {}
    lib._connector = gateway
    lib._data = {"rec": {"uri": "/recordings/x", "result": {}}}
    lib._main_data = {"id": "rec", "name": "rec"}
    lib._unit_of_measurement = "kWh"
    return lib


def _hours(**values):
    """24 hourly kWh values, e.g. _hours(h20=1.5) -> 20:00 has 1.5 kWh."""
    day = [0.0] * 24
    for key, value in values.items():
        day[int(key[1:])] = value
    return day


@pytest.mark.usefixtures("berlin_time_zone")
class TestCloseYesterdayWithTheRealLibrary:
    """Same runs against fetch_range and update of bosch-thermostat-client."""

    @staticmethod
    def _row(dt, state, _sum):
        return {"start": dt.timestamp(), "state": state, "sum": _sum}

    def _sensor(self, db_rows, gateway):
        sensor = _make_sensor(new_stats_api=True)
        sensor._statistic_import_lock = asyncio.Lock()
        sensor._bosch_object = _library_object(gateway)
        stat_id = sensor.statistic_id
        sensor.db_rows = db_rows

        async def stats_from_db(start_time, end_time):
            rows = [
                r
                for r in sensor.db_rows
                if start_time.timestamp() <= r["start"] < end_time.timestamp()
            ]
            return {stat_id: rows} if rows else {}

        async def last_stat():
            return {stat_id: [max(sensor.db_rows, key=lambda r: r["start"])]}

        sensor.get_last_stat = AsyncMock(side_effect=last_stat)
        sensor.get_stats_from_ha_db = AsyncMock(side_effect=stats_from_db)
        sensor.add_external_stats = MagicMock()
        return sensor

    async def _run(self, sensor, now, day_closed=False):
        """Hourly run like __init__.py: library update, then the sensor.

        Returns the written rows as {start as timestamp: (state, sum)}.
        """
        if day_closed:  # forces the path without the re-fetch (old code)
            sensor._day_closed = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        await sensor._bosch_object.update(time=now)
        sensor.add_external_stats.reset_mock()
        with patch("custom_components.bosch.sensor.recording.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.start_of_local_day.side_effect = lambda d=None: (
                (d or now).replace(hour=0, minute=0, second=0, microsecond=0)
            )
            await sensor.async_update()
        if not sensor.add_external_stats.called:
            return {}
        return {
            row["start"].timestamp(): (row["state"], round(row["sum"], 3))
            for row in sensor.add_external_stats.call_args.kwargs["stats"]
        }

    @pytest.mark.asyncio
    async def test_stale_library_value_inside_yesterday_is_dropped(self):
        """14:00 was fetched with 0.4 kWh once and ended at 0 kWh."""
        day = datetime(2026, 11, 14, tzinfo=TZ_BERLIN)
        y = datetime(2026, 11, 13, tzinfo=TZ_BERLIN)
        gateway = _FakeGateway(
            {
                "2026-11-13": _hours(h20=2.0),
                "2026-11-14": _hours(h0=0.5),
            }
        )
        sensor = self._sensor(
            [
                self._row(y - timedelta(hours=1), 1.0, 100.0),
                self._row(y + timedelta(hours=14), 0.4, 100.4),
            ],
            gateway,
        )
        stale = y + timedelta(hours=14)
        sensor._bosch_object._past_data[stale] = {"d": stale, "value": 0.4}

        written = await self._run(sensor, day.replace(hour=1, minute=6))

        assert written[stale.timestamp()] == (0, 100.0)
        assert written[(y + timedelta(hours=20)).timestamp()] == (2.0, 102.0)
        assert written[day.timestamp()] == (0.5, 102.5)

    @pytest.mark.parametrize(
        ("today", "yesterday_hours"),
        [
            # plain day; 14:00 had a partial value that ended at 0 kWh
            ((2026, 11, 14), _hours(h13=1.0, h20=2.0, h23=0.7)),
            # day of the spring clock change, the gateway numbers 0..23
            ((2026, 3, 30), _hours(h1=0.3, h2=0.7, h3=0.4, h23=1.1)),
            ((2026, 3, 30), _hours(h1=0.3, h2=0.7)),
            # day after the autumn clock change with 24 entries
            ((2026, 10, 26), _hours(h2=0.6, h3=0.2, h22=1.0)),
        ],
    )
    @pytest.mark.asyncio
    async def test_first_run_writes_what_the_midnight_run_writes(
        self, today, yesterday_hours
    ):
        """Re-fetch at 00:06 and at 01:06 == the old run right after midnight."""
        day = datetime(*today, tzinfo=TZ_BERLIN)
        y = day - timedelta(days=1)
        key = y.strftime("%Y-%m-%d")
        at_13 = yesterday_hours[13]  # HA went down at 14:30

        def sensor():
            gateway = _FakeGateway(
                {key: yesterday_hours, day.strftime("%Y-%m-%d"): _hours()}
            )
            return self._sensor(
                [
                    self._row(y - timedelta(hours=1), 1.0, 100.0),
                    self._row(y + timedelta(hours=13), at_13, 100.0 + at_13),
                    self._row(y + timedelta(hours=14), 0.3, 100.3 + at_13),
                ],
                gateway,
            )

        old = await self._run(
            sensor(), day.replace(minute=6), day_closed=True
        )
        at_midnight = sensor()
        new = await self._run(at_midnight, day.replace(minute=6))
        an_hour_later = sensor()
        late = await self._run(an_hour_later, day.replace(hour=1, minute=6))

        assert old, "the old path wrote nothing, the comparison is empty"
        assert new == old
        assert late == old
        assert at_midnight._day_closed == day
        assert an_hour_later._day_closed == day
        # the re-fetch asked for yesterday, exactly once
        assert an_hour_later._bosch_object._connector.requests.count(
            f"/recordings/x?interval={key}"
        ) == 1

    @pytest.mark.asyncio
    async def test_autumn_clock_change_day_with_25_entries(self):
        """Real library: ValueError for hour=24, the hour is still written."""
        day = datetime(2026, 10, 26, tzinfo=TZ_BERLIN)
        y = datetime(2026, 10, 25, tzinfo=TZ_BERLIN)
        gateway = _FakeGateway(
            {
                "2026-10-25": _hours(h20=2.0) + [1.0],
                "2026-10-26": _hours(h0=0.5),
            }
        )
        sensor = self._sensor(
            [
                self._row(y - timedelta(hours=1), 1.0, 100.0),
                self._row(y + timedelta(hours=14), 0.4, 100.4),
            ],
            gateway,
        )

        for hour in (1, 2, 3, 4):
            written = await self._run(sensor, day.replace(hour=hour, minute=6))
            assert written[day.timestamp()] == (0.5, 100.9), hour

        assert sensor._day_closed == day
        assert gateway.requests.count("/recordings/x?interval=2026-10-25") == 3

    @pytest.mark.asyncio
    async def test_empty_answer_is_asked_once_per_day(self):
        """Real library: empty answer = request loop until the timeout."""
        day = datetime(2026, 11, 14, tzinfo=TZ_BERLIN)
        y = datetime(2026, 11, 13, tzinfo=TZ_BERLIN)
        gateway = _FakeGateway({"2026-11-14": _hours(h0=0.5)})
        sensor = self._sensor(
            [
                self._row(y - timedelta(hours=1), 1.0, 100.0),
                self._row(y + timedelta(hours=14), 0.4, 100.4),
            ],
            gateway,
        )

        with patch(
            "custom_components.bosch.sensor.recording.CLOSE_DAY_FETCH_TIMEOUT",
            0.05,
        ):
            written = await asyncio.wait_for(
                self._run(sensor, day.replace(hour=1, minute=6)), 5
            )
            flood = len(gateway.requests)
            again = await asyncio.wait_for(
                self._run(sensor, day.replace(hour=2, minute=6)), 5
            )

        assert written[day.timestamp()] == (0.5, 100.9)
        assert again[day.timestamp()] == (0.5, 100.9)
        assert sensor._day_closed == day
        assert flood > 2
        assert len(gateway.requests) == flood + 1  # only the normal update

    def _complete_yesterday(self, y, day):
        """Recorder as the midnight run left it, plus today 00:00-09:00."""
        rows = [self._row(y - timedelta(hours=1), 1.0, 100.0)]
        total = 100.0
        for hour in range(24):
            total += 1.0
            rows.append(self._row(y + timedelta(hours=hour), 1.0, total))
        for hour in range(10):
            total += 0.5
            rows.append(self._row(day + timedelta(hours=hour), 0.5, total))
        return rows

    @pytest.mark.parametrize(
        "gateway_yesterday",
        [
            [0.0] * 24,  # the gateway lost its history
            [0.5] * 10 + [0.0] * 14,  # it answers with the rows of today
            [3.0] * 12 + [0.0] * 12,  # another day with more in total
            [1.0] * 12 + [0.9] + [1.0] * 11,  # one hour came back smaller
            None,  # answer without recordings
        ],
    )
    @pytest.mark.asyncio
    async def test_restart_keeps_a_complete_yesterday(self, gateway_yesterday):
        """Restart at 10:30, the gateway reports less than the recorder has."""
        day = datetime(2026, 11, 14, tzinfo=TZ_BERLIN)
        y = datetime(2026, 11, 13, tzinfo=TZ_BERLIN)
        gateway = _FakeGateway({"2026-11-14": [0.5] * 10 + [0.0] * 14})
        if gateway_yesterday is None:
            real_get = gateway.get

            async def get(uri):
                if uri.endswith("2026-11-13"):
                    gateway.requests.append(uri)
                    return {"interval": "2026-11-13"}
                return await real_get(uri)

            gateway.get = get
        else:
            gateway.days["2026-11-13"] = gateway_yesterday
        sensor = self._sensor(self._complete_yesterday(y, day), gateway)

        written = await self._run(sensor, day.replace(hour=10, minute=30))
        again = await self._run(sensor, day.replace(hour=11, minute=6))

        # 24 kWh of yesterday stay, nothing of yesterday is rewritten
        assert min(written) == day.timestamp()
        assert written[(day + timedelta(hours=9)).timestamp()] == (0.5, 129.0)
        assert again[(day + timedelta(hours=9)).timestamp()] == (0.5, 129.0)
        assert sensor._day_closed == day
        assert gateway.requests.count("/recordings/x?interval=2026-11-13") == 1

    @pytest.mark.asyncio
    async def test_restart_with_a_complete_yesterday_changes_nothing(self):
        """Same answer as the recorder has: same rows, same sums."""
        day = datetime(2026, 11, 14, tzinfo=TZ_BERLIN)
        y = datetime(2026, 11, 13, tzinfo=TZ_BERLIN)
        gateway = _FakeGateway(
            {
                "2026-11-13": [1.0] * 24,
                "2026-11-14": [0.5] * 10 + [0.0] * 14,
            }
        )
        db_rows = self._complete_yesterday(y, day)
        sensor = self._sensor(db_rows, gateway)

        written = await self._run(sensor, day.replace(hour=10, minute=30))

        assert min(written) == y.timestamp()  # yesterday was rebuilt
        for row in db_rows[1:]:
            assert written[row["start"]] == (row["state"], row["sum"])

    @pytest.mark.asyncio
    async def test_failed_first_run_does_not_block_the_second(self):
        """The 0 kWh rows of the normal path must not hide the partial hour."""
        from bosch_thermostat_client.exceptions import DeviceException

        day = datetime(2026, 11, 14, tzinfo=TZ_BERLIN)
        y = datetime(2026, 11, 13, tzinfo=TZ_BERLIN)
        gateway = _FakeGateway(
            {
                "2026-11-13": _hours(h20=1.0, h21=0.5, h22=1.2, h23=1.3),
                "2026-11-14": [0.5] * 10 + [0.0] * 14,
            }
        )
        real_get = gateway.get
        failures = [DeviceException("gateway hiccup")]

        async def get(uri):
            if uri.endswith("2026-11-13") and failures:
                raise failures.pop()
            return await real_get(uri)

        gateway.get = get
        # HA was down from 21:30 until 08:00; 21:00 is a partial value
        sensor = self._sensor(
            [
                self._row(y - timedelta(hours=1), 1.0, 100.0),
                self._row(y + timedelta(hours=20), 1.0, 101.0),
                self._row(y + timedelta(hours=21), 0.8, 101.8),
            ],
            gateway,
        )

        first = await self._run(sensor, day.replace(hour=8, minute=6))
        # the normal path wrote 22:00 and 23:00 as 0 kWh rows
        assert first[(y + timedelta(hours=23)).timestamp()] == (0, 101.8)
        rows = {row["start"]: row for row in sensor.db_rows}
        for start, (state, total) in first.items():
            rows[start] = {"start": start, "state": state, "sum": total}
        sensor.db_rows = sorted(rows.values(), key=lambda row: row["start"])
        second = await self._run(sensor, day.replace(hour=9, minute=6))

        assert second[(y + timedelta(hours=21)).timestamp()] == (0.5, 101.5)
        assert second[(y + timedelta(hours=22)).timestamp()] == (1.2, 102.7)
        assert second[(y + timedelta(hours=23)).timestamp()] == (1.3, 104.0)
        assert second[day.timestamp()] == (0.5, 104.5)
        assert sensor._day_closed == day

    @pytest.mark.asyncio
    async def test_zeros_do_not_replace_the_only_row_with_a_value(self):
        """Quiet day: all of yesterday is in its newest row."""
        day = datetime(2026, 11, 14, tzinfo=TZ_BERLIN)
        y = datetime(2026, 11, 13, tzinfo=TZ_BERLIN)
        gateway = _FakeGateway(
            {"2026-11-13": [0.0] * 24, "2026-11-14": _hours(h6=0.5)}
        )
        db_rows = [self._row(y - timedelta(hours=1), 1.0, 100.0)]
        db_rows += [
            self._row(y + timedelta(hours=hour), 0.0, 100.0)
            for hour in range(13)
        ]
        db_rows.append(self._row(y + timedelta(hours=13), 1.5, 101.5))
        sensor = self._sensor(db_rows, gateway)

        written = await self._run(sensor, day.replace(hour=10, minute=30))

        assert (y + timedelta(hours=13)).timestamp() not in written
        assert written[(day + timedelta(hours=6)).timestamp()] == (0.5, 102.0)
        assert sensor._day_closed == day

    @pytest.mark.asyncio
    async def test_restart_adds_what_the_gateway_has_on_top(self):
        """Hours that grew or came new are taken, 23:00 may shrink."""
        day = datetime(2026, 11, 14, tzinfo=TZ_BERLIN)
        y = datetime(2026, 11, 13, tzinfo=TZ_BERLIN)
        gateway = _FakeGateway(
            {
                "2026-11-13": [1.0] * 5 + [1.5] + [1.0] * 17 + [0.4],
                "2026-11-14": [0.5] * 10 + [0.0] * 14,
            }
        )
        sensor = self._sensor(self._complete_yesterday(y, day), gateway)

        written = await self._run(sensor, day.replace(hour=10, minute=30))

        assert written[(y + timedelta(hours=5)).timestamp()] == (1.5, 106.5)
        assert written[(y + timedelta(hours=23)).timestamp()] == (0.4, 123.9)
        assert written[(day + timedelta(hours=9)).timestamp()] == (0.5, 128.9)
