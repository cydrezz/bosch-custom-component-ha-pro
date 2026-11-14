"""Bosch sensor for Recording sensor in IVT."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from bosch_thermostat_client.exceptions import DeviceException
from homeassistant.components.recorder.models import (
    StatisticData,
    timestamp_to_datetime_or_none,
)
from homeassistant.util import dt as dt_util

from ..const import SIGNAL_RECORDING_UPDATE_BOSCH, UNITS_CONVERTER, VALUE
from .statistic_helper import StatisticHelper, StatisticsQueryError

_LOGGER = logging.getLogger(__name__)

# Re-fetching yesterday (see _close_yesterday) is tried in this many hourly
# runs per day, then the day is left as it is.
CLOSE_DAY_MAX_ATTEMPTS = 3
# Seconds the single request for yesterday may take.
CLOSE_DAY_FETCH_TIMEOUT = 20
# kWh an hour of yesterday may come back smaller than the recorder has it
CLOSE_DAY_TOLERANCE = 0.05


class RecordingSensor(StatisticHelper):
    """Representation of Recording Sensor."""

    signal = SIGNAL_RECORDING_UPDATE_BOSCH
    _domain_name = "Recording"
    # local midnight of the day on which yesterday was re-fetched (or given up)
    _day_closed = None
    # (local midnight, number of runs that tried to re-fetch yesterday)
    _close_day_attempts = (None, 0)

    @property
    def device_name(self) -> str:
        """Device name."""
        return "Recording sensors"

    @property
    def statistic_id(self) -> str:
        """External API statistic ID."""
        if not self._short_id:
            self._short_id = self.entity_id.replace(".", "").replace(
                "sensor", ""
            )
        return f"{self._domain_name}:{self._short_id}external".lower()

    def attrs_write(self, last_reset) -> None:
        """Entity attributes write."""
        self._unit_of_measurement = UNITS_CONVERTER.get(
            self._bosch_object.unit_of_measurement
        )
        self._attr_device_class = self._bosch_object.device_class
        self._attr_state_class = self._bosch_object.state_class

        if (
            self._attr_device_class == "temperature"
            and self._attr_state_class == "total"
        ):
            self._attr_state_class = "measurement"

        if self._attr_state_class == "total":
            self._attr_last_reset = last_reset
        if self._update_init:
            self._update_init = False
        self.async_schedule_update_ha_state()

    async def async_old_gather_update(self) -> None:
        """Old async update."""
        data = self._bosch_object.get_property(self._attr_uri)
        now = dt_util.now()
        if not data or not data.get(VALUE):
            _LOGGER.debug(
                "Recording %s: No data from bosch_object for uri %s. Raw data: %s",
                self.unique_id,
                self._attr_uri,
                data,
            )
            return

        def get_last_full_hour() -> datetime:
            return now - timedelta(hours=1)

        last_hour = get_last_full_hour().replace(
            minute=0, second=0, microsecond=0
        )

        def find_idx():
            for row in data[VALUE]:
                if row["d"] == last_hour:
                    return row.get(VALUE)

            # Fallback for slow gateways with processing delays:
            # Some gateways (e.g. KM100) write recording data with a 2-3 hour lag.
            # Instead of returning unavailable, use the latest available value.
            _LOGGER.debug(
                "Recording %s: Could not find %s in data. Last items: %s",
                self.unique_id,
                last_hour,
                [x["d"] for x in data[VALUE][-5:]] if data[VALUE] else "Empty",
            )
            if data[VALUE]:
                latest_entry = data[VALUE][-1]
                latest_date = latest_entry["d"]
                age = now - latest_date if latest_date else None
                if age and age <= timedelta(hours=6):
                    _LOGGER.debug(
                        "Recording %s: Using fallback value from %s (age: %s)",
                        self.unique_id,
                        latest_date,
                        age,
                    )
                    return latest_entry.get(VALUE)
                _LOGGER.warning(
                    "Recording %s: Latest available data from %s is too old (age: %s). "
                    "Check gateway clock synchronization.",
                    self.unique_id,
                    latest_date,
                    age,
                )
            return None

        self._state = find_idx()
        self.attrs_write(last_reset=last_hour)

    async def async_update(self) -> None:
        """Update state of device."""
        _LOGGER.debug("Update of sensor %s called.", self.unique_id)
        if self._new_stats_api:
            self._unit_of_measurement = UNITS_CONVERTER.get(
                self._bosch_object.unit_of_measurement
            )
            _LOGGER.debug(
                "Invoking external statistic function for %s.",
                self.statistic_id,
            )
            async with self._statistic_import_lock:
                try:
                    await self._insert_statistics()
                except StatisticsQueryError:
                    # DB query failed - retry next hour instead of
                    # re-importing 30 days with a fresh sum
                    return
                except (DeviceException, TimeoutError) as err:
                    # range fetch failed: nothing is written (no 0 kWh rows
                    # for hours the gateway did not deliver), retry next hour
                    _LOGGER.warning(
                        "Gateway delivered no recordings for %s, retry with "
                        "the next hourly run: %r",
                        self.statistic_id,
                        err,
                    )
        else:
            _LOGGER.debug("Old gather data algorithm.")
            await self.async_old_gather_update()

    async def _upsert_past_statistics(
        self, start: datetime, stop: datetime
    ) -> None:
        now = dt_util.now()
        diff = now - start
        if now.date() == start.date():
            _LOGGER.warning("Can't upsert today date. Try again tomorrow.")
            return
        if diff > timedelta(days=60):
            _LOGGER.warning(
                "Update more than 60 days might take some time! Component will try to do that anyway!"
            )
        stats = await self.fetch_past_data(
            start_time=start, stop_time=start + timedelta(hours=26)
        )  # return list of objects {'d': datetime with timezone, 'value': 'used kWh in last hour'}
        if not stats:
            _LOGGER.debug("No stats found. Exiting.")
            return
        stats_dict = {
            dt_util.as_timestamp(
                stat["d"].replace(minute=0, second=0, microsecond=0)
            ): stat
            for stat in stats.values()
        }
        # get stats from HA database
        last_stats = await self.get_stats_from_ha_db(
            start_time=start - timedelta(hours=1), end_time=now
        )
        last_stat = last_stats.get(self.statistic_id)
        _sum = last_stat[0].get("sum", 0) if last_stat else 0
        out = {}
        current_time = start
        while current_time <= stop:
            current_ts = dt_util.as_timestamp(current_time)
            if current_ts in stats_dict:
                stat = stats_dict[current_ts]
                _state = +stat["value"]
                _sum += _state  # increase sum
                _LOGGER.debug(
                    "Putting past state to statistic table with id: %s. Date: %s, state: %s, sum: %s.",
                    self.statistic_id,
                    current_time,
                    _state,
                    _sum,
                )
                out[current_ts] = StatisticData(
                    start=current_time,
                    state=_state,
                    sum=_sum,
                )
                stats_dict[current_ts] = None
            else:
                out[current_ts] = StatisticData(
                    start=current_time,
                    state=0,
                    sum=_sum,
                )
            current_time += timedelta(hours=1)

        if last_stat:
            start_ts = dt_util.as_timestamp(start)
            for stat in last_stat:
                _start = stat["start"]
                if stat["start"] in out or start_ts > _start:
                    continue
                _state = stat["state"]
                _sum += _state
                out[stat["start"]] = StatisticData(
                    start=dt_util.utc_from_timestamp(_start),
                    state=_state,
                    sum=_sum,
                )
        self.add_external_stats(stats=list(out.values()))

    def append_statistics(
        self,
        stats: list,
        sum: float,
        now: datetime,
        fill_from: datetime | None = None,
    ) -> float:
        """Push hourly rows and fill quiet hours with 0 kWh rows.

        The client library drops hours with 0 kWh, so a quiet hour has no
        row at all. The statistic card takes the row at or after (period
        start - 1 h) as baseline; without a 23:00 row it falls back to the
        first row of the day and loses that hour (card 9 kWh vs. bar chart
        11 kWh on 2026-10-31). Rows are sorted by hour, duplicates of an
        hour are skipped, and every hour from the hour after the baseline row
        (fill_from, if given) up to the newest data row without data is
        written with state 0 and an unchanged sum. Hours after the newest row
        stay open: they may be future hours of the current day.
        """
        statistics_to_push = []
        rows = sorted(
            (stat for stat in stats if stat["value"] != 0),
            key=lambda stat: stat["d"],
        )
        cursor = fill_from + timedelta(hours=1) if fill_from else None
        for stat in rows:
            _date: datetime = stat["d"]
            if cursor is not None and _date < cursor:
                continue  # duplicate hour
            while cursor is not None and cursor < _date:
                statistics_to_push.append(
                    StatisticData(start=cursor, state=0, sum=sum)
                )
                cursor += timedelta(hours=1)
            _state = stat["value"]
            sum += _state
            _LOGGER.debug(
                "Appending day to statistic table with id: %s. Date: %s, state: %s, sum: %s.",
                self.statistic_id,
                _date,
                _state,
                sum,
            )
            statistics_to_push.append(
                StatisticData(
                    start=_date,
                    state=_state,
                    sum=sum,
                )
            )
            cursor = _date + timedelta(hours=1)
        self.add_external_stats(stats=statistics_to_push)
        self._last_reset = now
        return sum

    async def _close_yesterday(
        self, last_stats: dict, start_of_day: datetime, now: datetime
    ) -> tuple[list, float, datetime] | None:
        """Re-fetch yesterday once per day and rebuild from the row before it.

        The client library fetches the previous day only in the run right
        after midnight. If that run is missed (gateway or HA down), the last
        hours of yesterday never arrive and append_statistics fills them with
        0 kWh for good. So the first run of a day, and the first run after a
        restart, asks the gateway for yesterday itself.

        The result is what the run right after midnight would have written:
        yesterday as the gateway reports it now (an hour it leaves out is a
        0 kWh hour, the library cannot tell the two apart), today from the
        normal state.

        The re-fetch may add to yesterday, never take away: fetch_range does
        not check which day the gateway answered with, and a gateway that
        lost its history answers with zeros. If the answer has less for an
        hour than the recorder already holds, yesterday is left as it is.
        Only the newest row of yesterday with a value may shrink: it can be
        the partial value of an hour that was still running when the hourly
        runs stopped. An answer without any row for yesterday replaces
        nothing.

        Returns (rows, sum, baseline start) for append_statistics, or None:
        then the caller writes this hour the normal way. Nothing in here may
        stop the hourly write. Every error except a failed recorder query
        ends in None, and after CLOSE_DAY_MAX_ATTEMPTS runs, or after one
        timeout, the day is left as it is.
        """
        if self._close_day_attempts[0] != start_of_day:
            self._close_day_attempts = (start_of_day, 0)
        attempt = self._close_day_attempts[1] + 1
        self._close_day_attempts = (start_of_day, attempt)
        if attempt >= CLOSE_DAY_MAX_ATTEMPTS:
            # set before anything can fail: there is no further attempt today
            self._day_closed = start_of_day
        try:
            yesterday = start_of_day - timedelta(days=1)
            baseline = await self.async_get_last_stats_before_date(
                last_stats=last_stats, day=yesterday
            )
            if not baseline:
                # no row before yesterday, nothing to rebuild from
                self._day_closed = start_of_day
                return None
            base_start = baseline.get("start")
            if isinstance(base_start, float):
                base_start = timestamp_to_datetime_or_none(base_start)
            state = self._bosch_object.state
            today = [
                row
                for row in (state if isinstance(state, list) else [])
                if row["d"] >= start_of_day
            ]
            covered_until = max(
                (row["d"].timestamp() for row in today),
                default=start_of_day.timestamp() - 1,
            )
            recorded = (
                await self.get_stats_from_ha_db(
                    start_time=yesterday, end_time=now
                )
            ).get(self.statistic_id, [])
            day_ts = start_of_day.timestamp()
            if any(stat["start"] > covered_until for stat in recorded):
                # a row of today behind the newest state row would keep its
                # old sum after yesterday changed; wait for a later run
                _LOGGER.debug(
                    "Rows of today for %s are newer than the gateway state, "
                    "yesterday is not re-fetched in this run.",
                    self.statistic_id,
                )
                return None
            bosch_data = await self.fetch_past_data(
                start_time=yesterday,
                stop_time=start_of_day,
                timeout=CLOSE_DAY_FETCH_TIMEOUT,
            )
            rows = [
                row
                for row in list(bosch_data.values())
                if yesterday <= row["d"] < start_of_day
            ]
            fetched: dict[float, float] = {}
            for row in rows:
                hour = row["d"].timestamp()
                fetched[hour] = max(fetched.get(hour, 0), row[VALUE])
            # rows with a value only: the normal path may have written
            # 0 kWh rows behind the partial hour in an earlier run
            had = sorted(
                (stat["start"], stat.get("state") or 0)
                for stat in recorded
                if stat["start"] < day_ts and (stat.get("state") or 0) > 0
            )
            lost = [
                (hour, value)
                for hour, value in (had[:-1] if rows else had)
                if fetched.get(hour, 0) < value - CLOSE_DAY_TOLERANCE
            ]
            if lost:
                self._day_closed = start_of_day
                hour, value = lost[0]
                _LOGGER.warning(
                    "Gateway reports less for yesterday of %s than the "
                    "recorder holds (%s hours, first %s: %s < %s), "
                    "yesterday is left as it is.",
                    self.statistic_id,
                    len(lost),
                    timestamp_to_datetime_or_none(hour),
                    fetched.get(hour, 0),
                    value,
                )
                return None
            rows.extend(today)
        except StatisticsQueryError:
            raise
        except TimeoutError as err:
            # On an empty answer the library repeats the request without a
            # pause until the timeout. Not a second time today.
            self._day_closed = start_of_day
            _LOGGER.warning(
                "No answer for yesterday of %s within %s s, the day is left "
                "as it is and this hour is written the normal way: %r",
                self.statistic_id,
                CLOSE_DAY_FETCH_TIMEOUT,
                err,
            )
            return None
        except Exception as err:  # noqa: BLE001
            # deliberately broad: the client library raises ValueError for a
            # day with 25 entries. Nothing here may stop the hourly write.
            _LOGGER.warning(
                "Could not re-fetch yesterday for %s (run %s of %s), this "
                "hour is written the normal way: %r",
                self.statistic_id,
                attempt,
                CLOSE_DAY_MAX_ATTEMPTS,
                err,
            )
            return None
        self._day_closed = start_of_day
        return rows, baseline.get("sum", 0), base_start

    async def _insert_statistics(self) -> None:
        """Insert external statistics."""
        _sum = 0
        now = dt_util.now()
        last_stat = await self.get_last_stat()
        if len(last_stat) == 0 or len(last_stat[self.statistic_id]) == 0:
            _LOGGER.debug(
                "Last stats not exist. Trying to fetch last 30 days of data."
            )
            start_time = now - timedelta(days=30)
            all_stats = await self.fetch_past_data(
                start_time=start_time, stop_time=now
            )
            if not all_stats:
                _LOGGER.debug("30 days stats not found. Trying 1 day.")
                start_time = now - timedelta(days=1)
                all_stats = await self.fetch_past_data(
                    start_time=start_time, stop_time=now
                )
            if not all_stats:
                _LOGGER.debug("Stats not found.")
                return
            all_stats = list(all_stats.values())
            self.append_statistics(stats=all_stats, sum=_sum, now=now)
            return

        self._state = last_stat[self.statistic_id][0].get("state")
        self.async_schedule_update_ha_state()
        start_of_day = dt_util.start_of_local_day()
        last_stat_row = last_stat[self.statistic_id][0]
        last_stat_start = timestamp_to_datetime_or_none(
            last_stat_row.get("start")
        )

        async def get_last_stats_in_ha():
            start_time = dt_util.start_of_local_day(
                last_stat_start
            ) - timedelta(hours=24)
            return await self.get_stats_from_ha_db(
                start_time=start_time,
                end_time=now,
            )

        last_stats = await get_last_stats_in_ha()

        if not last_stats:
            last_stats = last_stat

        all_stats = []

        async def get_last_stats_from_bosch_api():
            last_stats_row = await self.async_get_last_stats_before_date(
                last_stats=last_stats, day=start_of_day
            ) or {}
            start_time = last_stats_row.get("start")
            _sum = last_stats_row.get("sum", 0)
            if isinstance(start_time, float):
                start_time = timestamp_to_datetime_or_none(start_time)
            if not start_time:
                _LOGGER.debug(
                    "Start time not found. %s found %s",
                    self.statistic_id,
                    start_time,
                )
            elif start_time.date() < now.date() - timedelta(days=1):
                diff = now - start_time
                _LOGGER.debug(
                    "Last row of statistic %s found %s, missing %s with current sum %s",
                    self.statistic_id,
                    start_time,
                    diff,
                    _sum,
                )
                bosch_data = await self.fetch_past_data(
                    start_time=start_time, stop_time=now
                )
                # this fetch covered yesterday as well
                self._day_closed = start_of_day
                return (
                    [
                        row
                        for row in bosch_data.values()
                        if row["d"] > start_time
                    ],
                    _sum,
                    start_time,
                )
            elif self._day_closed != start_of_day:
                closed = await self._close_yesterday(
                    last_stats, start_of_day, now
                )
                if closed is not None:
                    return closed
            _LOGGER.debug(
                "Returning state to put to statistic table %s",
                self._bosch_object.state,
            )
            if self._bosch_object.state:
                # let's get last state once again
                # as bosch state provide whole day always.
                first_date_from_state = self._bosch_object.state[0]
                last_stats_row = await self.async_get_last_stats_before_date(
                    last_stats=last_stats, day=first_date_from_state["d"]
                ) or {}
                _sum = last_stats_row.get("sum", 0)
                start_time = last_stats_row.get("start")
                if isinstance(start_time, float):
                    start_time = timestamp_to_datetime_or_none(start_time)
            return self._bosch_object.state, _sum, start_time

        if self.statistic_id in last_stats:
            all_stats, _sum, _base = await get_last_stats_from_bosch_api()
            self.append_statistics(
                stats=all_stats, sum=_sum, now=now, fill_from=_base
            )
        else:
            _LOGGER.debug(
                "Statistic ID %s not found in last_stats keys: %s. "
                "Falling back to last_stat.",
                self.statistic_id,
                list(last_stats.keys()),
            )
            # last_stat is guaranteed to contain self.statistic_id at this point
            # (checked at method entry). Use it as fallback.
            last_stats = last_stat
            if self.statistic_id in last_stats:
                all_stats, _sum, _base = await get_last_stats_from_bosch_api()
                self.append_statistics(
                    stats=all_stats, sum=_sum, now=now, fill_from=_base
                )
