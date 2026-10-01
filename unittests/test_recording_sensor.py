"""Tests for Recording Sensor fallback logic and _insert_statistics."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
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

        with patch("custom_components.bosch.sensor.recording.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.start_of_local_day.return_value = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            await sensor._insert_statistics()

        sensor.append_statistics.assert_called_once()

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
