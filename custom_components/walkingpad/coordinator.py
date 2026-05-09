"""Coordinator: BLE connection lifecycle, polling, cumulative + periodic accumulation."""
from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from bleak_retry_connector import BleakClient, establish_connection
from ph4_walkingpad.pad import Controller, WalkingPadCurStatus

from .const import (
    DOMAIN,
    POLL_INTERVAL_SECONDS,
    RECONNECT_BACKOFF_SECONDS,
    STATUS_TIMEOUT_SECONDS,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
    WALKINGPAD_NOTIFY_UUID,
    WALKINGPAD_WRITE_UUID,
)

_LOGGER = logging.getLogger(__name__)


class WalkingPadCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Owns the BLE Controller and exposes polled + cumulative + daily + monthly data."""

    def __init__(self, hass: HomeAssistant, address: str, entry_id: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLL_INTERVAL_SECONDS),
        )
        self.address = address.upper()
        self._entry_id = entry_id

        self._controller: Controller | None = None
        self._connect_lock = asyncio.Lock()
        self._connected = False
        self._closing = False
        self._last_attempt_ts: float = 0.0
        self._first_advertisement_logged = False
        # Last time the pad sent us a status notification (monotonic).
        # Used as a watchdog to detect silent BLE link drops.
        self._last_status_ts: float = 0.0

        # Latest reported speed (km/h)
        self.speed_kmh: float = 0.0

        # Cumulative totals (lifetime, persisted).
        # Note: the pad reports distance in 10-meter units (1 unit = 0.01 km).
        self.total_steps: int = 0
        self.total_time_s: int = 0
        self.total_dist_dam: int = 0

        # Daily counters (reset at local midnight, persisted)
        self.daily_steps: int = 0
        self.daily_time_s: int = 0
        self.daily_dist_dam: int = 0
        self.daily_last_reset: datetime | None = None

        # Monthly counters (reset on the 1st at local midnight, persisted)
        self.monthly_steps: int = 0
        self.monthly_time_s: int = 0
        self.monthly_dist_dam: int = 0
        self.monthly_last_reset: datetime | None = None

        # Last seen session values (for delta computation)
        self._last_steps: int = 0
        self._last_time: int = 0
        self._last_dist: int = 0

        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}_{entry_id}"
        )
        self._unsub_bt = None
        self._unsub_midnight = None
        self._unsub_reconnect_timer = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def async_setup(self) -> None:
        """Load persisted totals, register BT callback, attempt initial connect."""
        await self._async_load_totals()

        # Catch up on any missed daily/monthly resets while HA was off.
        self._maybe_reset_periodic(dt_util.now())

        self._unsub_midnight = async_track_time_change(
            self.hass, self._on_midnight, hour=0, minute=0, second=0
        )

        self._unsub_bt = bluetooth.async_register_callback(
            self.hass,
            self._on_advertisement,
            BluetoothCallbackMatcher(address=self.address),
            BluetoothScanningMode.PASSIVE,
        )

        # Seed coordinator data so sensors render before any poll runs.
        self.async_set_updated_data(self._build_data())

        present = bluetooth.async_address_present(
            self.hass, self.address, connectable=True
        )
        _LOGGER.info(
            "WalkingPad %s: BT callback registered. Device currently visible to HA: %s",
            self.address,
            present,
        )
        if present:
            self.hass.async_create_task(self._async_connect())

    async def async_shutdown(self) -> None:
        self._closing = True
        if self._unsub_bt is not None:
            self._unsub_bt()
            self._unsub_bt = None
        if self._unsub_midnight is not None:
            self._unsub_midnight()
            self._unsub_midnight = None
        self._cancel_reconnect_timer()
        await self._async_disconnect()
        await self._async_save_totals()

    # ---- Bluetooth callback ------------------------------------------------

    @callback
    def _on_advertisement(
        self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        if not self._first_advertisement_logged:
            _LOGGER.info(
                "WalkingPad %s: advertisement received (name=%r, rssi=%s, connected=%s)",
                self.address,
                service_info.name,
                service_info.rssi,
                self._connected,
            )
            self._first_advertisement_logged = True
        else:
            _LOGGER.debug(
                "WalkingPad %s: advertisement (rssi=%s, connected=%s)",
                self.address,
                service_info.rssi,
                self._connected,
            )
        if self._connected or self._closing:
            return
        self.hass.async_create_task(self._async_connect())

    # ---- Connect / disconnect ---------------------------------------------

    async def _async_connect(self) -> None:
        if self._connected or self._closing:
            return

        now = _time.monotonic()
        if now - self._last_attempt_ts < RECONNECT_BACKOFF_SECONDS:
            _LOGGER.debug(
                "WalkingPad %s: connect attempt skipped (backoff %.1fs remaining)",
                self.address,
                RECONNECT_BACKOFF_SECONDS - (now - self._last_attempt_ts),
            )
            return
        self._last_attempt_ts = now

        async with self._connect_lock:
            if self._connected or self._closing:
                return

            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if ble_device is None:
                _LOGGER.info(
                    "WalkingPad %s: BLEDevice not available from HA cache yet",
                    self.address,
                )
                return
            _LOGGER.info(
                "WalkingPad %s: attempting connection (device=%r)",
                self.address,
                ble_device,
            )

            ctrl = Controller()
            ctrl.handler_cur_status = self._on_status

            # Use HA's slot-aware connection helper instead of letting the
            # ph4_walkingpad library create a raw BleakClient. This integrates
            # with HA's BT slot manager, retries on transient failures, and
            # avoids leaking slots after the pad powers off.
            try:
                client = await establish_connection(
                    BleakClient,
                    ble_device,
                    f"WalkingPad {self.address}",
                    disconnected_callback=self._on_ble_disconnect,
                    max_attempts=3,
                )
            except Exception as err:  # noqa: BLE001 — bleak raises a wide variety
                _LOGGER.warning(
                    "Connection to WalkingPad %s failed: %s", self.address, err
                )
                return

            ctrl.client = client

            # Locate the pad's notify (fe01) and write (fe02) characteristics.
            for service in client.services:
                for char in service.characteristics:
                    uuid = str(char.uuid).lower()
                    if uuid == WALKINGPAD_NOTIFY_UUID:
                        ctrl.char_fe01 = char
                    elif uuid == WALKINGPAD_WRITE_UUID:
                        ctrl.char_fe02 = char

            if ctrl.char_fe01 is None or ctrl.char_fe02 is None:
                _LOGGER.warning(
                    "WalkingPad %s: required GATT characteristics not found",
                    self.address,
                )
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                return

            try:
                await client.start_notify(ctrl.char_fe01, ctrl.notif_handler)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "WalkingPad %s: start_notify failed: %s", self.address, err
                )
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                return

            self._controller = ctrl
            self._connected = True
            # New BLE session: pad's session counters start at 0.
            self._last_steps = 0
            self._last_time = 0
            self._last_dist = 0
            # Seed the watchdog so we don't immediately declare a fresh
            # connection dead.
            self._last_status_ts = _time.monotonic()

            # We're connected; stop the periodic reconnect probe.
            self._cancel_reconnect_timer()

            _LOGGER.info("Connected to WalkingPad %s", self.address)
            self.async_set_updated_data(self._build_data())

    @callback
    def _on_ble_disconnect(self, _client: Any) -> None:
        """Called by bleak when the BLE link drops."""
        if not self._connected:
            return
        _LOGGER.info("WalkingPad %s: BLE disconnected", self.address)
        self._connected = False
        self.speed_kmh = 0.0
        # Allow an immediate reconnect on the next advertisement.
        self._last_attempt_ts = 0.0
        self._first_advertisement_logged = False
        self.async_set_updated_data(self._build_data())
        self.hass.async_create_task(self._async_cleanup_controller())
        self._schedule_reconnect_timer()

    async def _async_cleanup_controller(self) -> None:
        if self._controller is not None:
            try:
                await asyncio.wait_for(self._controller.disconnect(), timeout=5.0)
            except (Exception, asyncio.TimeoutError):  # noqa: BLE001
                pass
            self._controller = None

    async def _async_disconnect(self) -> None:
        if self._controller is not None:
            try:
                await asyncio.wait_for(self._controller.disconnect(), timeout=5.0)
            except (Exception, asyncio.TimeoutError):  # noqa: BLE001
                pass
            self._controller = None
        if self._connected:
            self._connected = False
            self.speed_kmh = 0.0
            # Allow an immediate reconnect on the next advertisement.
            self._last_attempt_ts = 0.0
            # Reset so the next reappearance is logged at INFO level.
            self._first_advertisement_logged = False
            _LOGGER.info(
                "WalkingPad %s: marked disconnected, awaiting next advertisement",
                self.address,
            )
            self.async_set_updated_data(self._build_data())
        # Whether or not we changed state, make sure a reconnect probe is armed
        # so we don't depend solely on the BT advertisement callback.
        self._schedule_reconnect_timer()

    # ---- Periodic reconnect probe -----------------------------------------

    def _schedule_reconnect_timer(self) -> None:
        if self._unsub_reconnect_timer is not None or self._closing:
            return
        self._unsub_reconnect_timer = async_track_time_interval(
            self.hass,
            self._async_reconnect_tick,
            timedelta(seconds=30),
        )

    def _cancel_reconnect_timer(self) -> None:
        if self._unsub_reconnect_timer is not None:
            self._unsub_reconnect_timer()
            self._unsub_reconnect_timer = None

    async def _async_reconnect_tick(self, _now: datetime) -> None:
        if self._connected or self._closing:
            self._cancel_reconnect_timer()
            return
        _LOGGER.debug(
            "WalkingPad %s: periodic reconnect probe", self.address
        )
        # Nudge HA's BT integration in case it's holding a stale cache for
        # this address (no fresh advertisement since the disconnect).
        rediscover = getattr(bluetooth, "async_rediscover_address", None)
        if callable(rediscover):
            try:
                rediscover(self.hass, self.address)
            except Exception:  # noqa: BLE001
                pass
        await self._async_connect()

    # ---- Status handling --------------------------------------------------

    @callback
    def _on_status(self, sender: Any, status: WalkingPadCurStatus) -> None:
        self._last_status_ts = _time.monotonic()

        d_steps, self._last_steps = self._delta(status.steps, self._last_steps)
        d_time, self._last_time = self._delta(status.time, self._last_time)
        d_dist, self._last_dist = self._delta(status.dist, self._last_dist)

        # Defensive: if a midnight reset was missed (e.g. HA was sleeping),
        # catch up before applying deltas to the daily/monthly buckets.
        self._maybe_reset_periodic(dt_util.now())

        self.total_steps += d_steps
        self.total_time_s += d_time
        self.total_dist_dam += d_dist

        self.daily_steps += d_steps
        self.daily_time_s += d_time
        self.daily_dist_dam += d_dist

        self.monthly_steps += d_steps
        self.monthly_time_s += d_time
        self.monthly_dist_dam += d_dist

        self.speed_kmh = status.speed / 10.0

        if d_steps or d_time or d_dist:
            self.hass.async_create_task(self._async_save_totals())

        self.async_set_updated_data(self._build_data())

    @staticmethod
    def _delta(current: int, last: int) -> tuple[int, int]:
        """Compute delta and new last-seen value, handling session resets.

        The pad resets its session counter to 0 when paused. If `current`
        drops below `last`, treat the new value as a fresh contribution.
        """
        if current < last:
            return current, current
        return current - last, current

    def _build_data(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "speed": self.speed_kmh,
            "total_steps": self.total_steps,
            "total_time_s": self.total_time_s,
            "total_dist_km": self.total_dist_dam / 100.0,
            "daily_steps": self.daily_steps,
            "daily_time_s": self.daily_time_s,
            "daily_dist_km": self.daily_dist_dam / 100.0,
            "daily_last_reset": self.daily_last_reset,
            "monthly_steps": self.monthly_steps,
            "monthly_time_s": self.monthly_time_s,
            "monthly_dist_km": self.monthly_dist_dam / 100.0,
            "monthly_last_reset": self.monthly_last_reset,
        }

    # ---- Polling ----------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        if not self._connected or self._controller is None:
            return self._build_data()

        client = self._controller.client
        if client is None or not client.is_connected:
            _LOGGER.info(
                "WalkingPad %s: BLE link is down, marking disconnected", self.address
            )
            await self._async_disconnect()
            return self._build_data()

        # Watchdog: BlueZ may keep is_connected=True for the full link
        # supervision timeout (5-20s) after the peer goes away. If we haven't
        # received any status notification for a while, treat the link as dead.
        silence = _time.monotonic() - self._last_status_ts
        if silence > STATUS_TIMEOUT_SECONDS:
            _LOGGER.info(
                "WalkingPad %s: no status received in %.1fs, treating as disconnected",
                self.address,
                silence,
            )
            await self._async_disconnect()
            return self._build_data()

        try:
            await self._controller.ask_stats()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Lost connection to WalkingPad %s: %s", self.address, err
            )
            await self._async_disconnect()
            raise UpdateFailed(str(err)) from err
        return self._build_data()

    # ---- Periodic resets --------------------------------------------------

    @callback
    def _on_midnight(self, _now: datetime) -> None:
        """Fired by HA at local 00:00:00 every day."""
        self._maybe_reset_periodic(dt_util.now())
        self.hass.async_create_task(self._async_save_totals())
        self.async_set_updated_data(self._build_data())

    def _maybe_reset_periodic(self, now: datetime) -> None:
        """Reset daily/monthly buckets if we've crossed their boundary."""
        if self.daily_last_reset is None or self.daily_last_reset.date() != now.date():
            self.daily_steps = 0
            self.daily_time_s = 0
            self.daily_dist_dam = 0
            self.daily_last_reset = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        if (
            self.monthly_last_reset is None
            or self.monthly_last_reset.year != now.year
            or self.monthly_last_reset.month != now.month
        ):
            self.monthly_steps = 0
            self.monthly_time_s = 0
            self.monthly_dist_dam = 0
            self.monthly_last_reset = now.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )

    # ---- Persistence ------------------------------------------------------

    async def _async_load_totals(self) -> None:
        data = await self._store.async_load() or {}
        self.total_steps = int(data.get("total_steps", 0))
        self.total_time_s = int(data.get("total_time_s", 0))
        self.total_dist_dam = int(data.get("total_dist_dam", 0))

        self.daily_steps = int(data.get("daily_steps", 0))
        self.daily_time_s = int(data.get("daily_time_s", 0))
        self.daily_dist_dam = int(data.get("daily_dist_dam", 0))
        self.daily_last_reset = _parse_dt(data.get("daily_last_reset"))

        self.monthly_steps = int(data.get("monthly_steps", 0))
        self.monthly_time_s = int(data.get("monthly_time_s", 0))
        self.monthly_dist_dam = int(data.get("monthly_dist_dam", 0))
        self.monthly_last_reset = _parse_dt(data.get("monthly_last_reset"))

    async def _async_save_totals(self) -> None:
        await self._store.async_save(
            {
                "total_steps": self.total_steps,
                "total_time_s": self.total_time_s,
                "total_dist_dam": self.total_dist_dam,
                "daily_steps": self.daily_steps,
                "daily_time_s": self.daily_time_s,
                "daily_dist_dam": self.daily_dist_dam,
                "daily_last_reset": self.daily_last_reset.isoformat()
                if self.daily_last_reset
                else None,
                "monthly_steps": self.monthly_steps,
                "monthly_time_s": self.monthly_time_s,
                "monthly_dist_dam": self.monthly_dist_dam,
                "monthly_last_reset": self.monthly_last_reset.isoformat()
                if self.monthly_last_reset
                else None,
            }
        )


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    return dt_util.parse_datetime(raw)
