"""WalkingPad sensors."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength, UnitOfSpeed, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WalkingPadCoordinator


@dataclass(frozen=True, kw_only=True)
class WalkingPadSensorDescription(SensorEntityDescription):
    """Describes a WalkingPad sensor and how to read it from coordinator data."""

    data_key: str
    requires_connection: bool = False
    period: str = ""  # "" | "daily" | "monthly"


def _steps(key: str, period: str = "") -> WalkingPadSensorDescription:
    suffix = f"_{period}" if period else ""
    state_class = (
        SensorStateClass.TOTAL if period else SensorStateClass.TOTAL_INCREASING
    )
    return WalkingPadSensorDescription(
        key=key,
        translation_key=key,
        data_key=f"{period or 'total'}_steps",
        native_unit_of_measurement="steps",
        state_class=state_class,
        period=period,
    )


def _time(key: str, period: str = "") -> WalkingPadSensorDescription:
    state_class = (
        SensorStateClass.TOTAL if period else SensorStateClass.TOTAL_INCREASING
    )
    return WalkingPadSensorDescription(
        key=key,
        translation_key=key,
        data_key=f"{period or 'total'}_time_s",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=state_class,
        period=period,
    )


def _distance(key: str, period: str = "") -> WalkingPadSensorDescription:
    state_class = (
        SensorStateClass.TOTAL if period else SensorStateClass.TOTAL_INCREASING
    )
    return WalkingPadSensorDescription(
        key=key,
        translation_key=key,
        data_key=f"{period or 'total'}_dist_km",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=state_class,
        suggested_display_precision=2,
        period=period,
    )


SENSORS: tuple[WalkingPadSensorDescription, ...] = (
    WalkingPadSensorDescription(
        key="speed",
        translation_key="speed",
        data_key="speed",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        requires_connection=True,
    ),
    # Lifetime totals
    _steps("total_steps"),
    _time("total_time"),
    _distance("total_distance"),
    # Daily counters
    _steps("daily_steps", "daily"),
    _time("daily_time", "daily"),
    _distance("daily_distance", "daily"),
    # Monthly counters
    _steps("monthly_steps", "monthly"),
    _time("monthly_time", "monthly"),
    _distance("monthly_distance", "monthly"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WalkingPadCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        WalkingPadSensor(coordinator, entry, desc) for desc in SENSORS
    )


class WalkingPadSensor(CoordinatorEntity[WalkingPadCoordinator], SensorEntity):
    """A single WalkingPad sensor."""

    _attr_has_entity_name = True
    entity_description: WalkingPadSensorDescription

    def __init__(
        self,
        coordinator: WalkingPadCoordinator,
        entry: ConfigEntry,
        description: WalkingPadSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="KingSmith",
            model="WalkingPad",
            connections={("bluetooth", coordinator.address)},
        )

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self.entity_description.data_key)

    @property
    def last_reset(self):
        if self.entity_description.period and self.coordinator.data is not None:
            return self.coordinator.data.get(
                f"{self.entity_description.period}_last_reset"
            )
        return None

    @property
    def available(self) -> bool:
        if self.entity_description.requires_connection:
            return self.coordinator.connected
        return True
