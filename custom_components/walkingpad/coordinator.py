"""Coordinator: BLE connection lifecycle, polling, cumulative accumulation."""
from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import timedelta
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ph4_walkingpad.pad import Controller, WalkingPadCurStatus

from .const import (
    DOMAIN,
    POLL_INTERVAL_SECONDS,
    RECONNECT_BACKOFF_SECONDS,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


class WalkingPadCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Owns the BLE Controller and exposes polled + cumulative data."""

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

        # Latest reported speed (km/h)
        self.speed_kmh: float = 0.0

        # Cumulative totals (persisted)
        self.total_steps: int = 0
        self.total_time_s: int = 0
        self.total_dist_cm: int = 0

        # Last seen session values (for delta computation)
        self._last_steps: int = 0
        self._last_time: int = 0
        self._last_dist: int = 0

        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}_{entry_id}"
        )
        self._unsub_bt = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def async_setup(self) -> None:
        """Load persisted totals, register BT callback, attempt initial connect."""
        await self._async_load_totals()

        self._unsub_bt = bluetooth.async_register_callback(
            self.hass,
            self._on_advertisement,
            BluetoothCallbackMatcher(address=self.address),
            BluetoothScanningMode.PASSIVE,
        )

        # Seed coordinator data so sensors render before any poll runs.
        self.async_set_updated_data(self._build_data())

        if bluetooth.async_address_present(self.hass, self.address, connectable=True):
            self.hass.async_create_task(self._async_connect())

    async def async_shutdown(self) -> None:
        self._closing = True
        if self._unsub_bt is not None:
            self._unsub_bt()
            self._unsub_bt = None
        await self._async_disconnect()
        await self._async_save_totals()

    # ---- Bluetooth callback ------------------------------------------------

    @callback
    def _on_advertisement(
        self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        if self._connected or self._closing:
            return
        self.hass.async_create_task(self._async_connect())

    # ---- Connect / disconnect ---------------------------------------------

    async def _async_connect(self) -> None:
        if self._connected or self._closing:
            return

        now = _time.monotonic()
        if now - self._last_attempt_ts < RECONNECT_BACKOFF_SECONDS:
            return
        self._last_attempt_ts = now

        async with self._connect_lock:
            if self._connected or self._closing:
                return

            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if ble_device is None:
                _LOGGER.debug("Device %s not currently visible", self.address)
                return

            ctrl = Controller()
            ctrl.handler_cur_status = self._on_status

            try:
                await ctrl.run(ble_device)
            except Exception as err:  # noqa: BLE001 — bleak raises a wide variety
                _LOGGER.warning(
                    "Connection to WalkingPad %s failed: %s", self.address, err
                )
                try:
                    await ctrl.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                return

            self._controller = ctrl
            self._connected = True
            # New BLE session: pad's session counters start at 0.
            self._last_steps = 0
            self._last_time = 0
            self._last_dist = 0
            _LOGGER.info("Connected to WalkingPad %s", self.address)
            self.async_set_updated_data(self._build_data())

    async def _async_disconnect(self) -> None:
        if self._controller is not None:
            try:
                await self._controller.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._controller = None
        if self._connected:
            self._connected = False
            self.speed_kmh = 0.0
            self.async_set_updated_data(self._build_data())

    # ---- Status handling --------------------------------------------------

    @callback
    def _on_status(self, sender: Any, status: WalkingPadCurStatus) -> None:
        d_steps, self._last_steps = self._delta(status.steps, self._last_steps)
        d_time, self._last_time = self._delta(status.time, self._last_time)
        d_dist, self._last_dist = self._delta(status.dist, self._last_dist)

        self.total_steps += d_steps
        self.total_time_s += d_time
        self.total_dist_cm += d_dist
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
            "total_dist_km": self.total_dist_cm / 100_000.0,
        }

    # ---- Polling ----------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        if not self._connected or self._controller is None:
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

    # ---- Persistence ------------------------------------------------------

    async def _async_load_totals(self) -> None:
        data = await self._store.async_load() or {}
        self.total_steps = int(data.get("total_steps", 0))
        self.total_time_s = int(data.get("total_time_s", 0))
        self.total_dist_cm = int(data.get("total_dist_cm", 0))

    async def _async_save_totals(self) -> None:
        await self._store.async_save(
            {
                "total_steps": self.total_steps,
                "total_time_s": self.total_time_s,
                "total_dist_cm": self.total_dist_cm,
            }
        )
