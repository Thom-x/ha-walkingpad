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
    WalkingPadSensorDescription(
        key="total_steps",
        translation_key="total_steps",
        data_key="total_steps",
        native_unit_of_measurement="steps",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:walk",
    ),
    WalkingPadSensorDescription(
        key="total_time",
        translation_key="total_time",
        data_key="total_time_s",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    WalkingPadSensorDescription(
        key="total_distance",
        translation_key="total_distance",
        data_key="total_dist_km",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
    ),
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
    def available(self) -> bool:
        if self.entity_description.requires_connection:
            return self.coordinator.connected
        # Cumulative totals stay available across disconnects (persisted).
        return True
