"""Config flow: Bluetooth discovery + manual address entry."""
from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS
from homeassistant.data_entry_flow import FlowResult

from .const import DEFAULT_NAME, DOMAIN

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def _normalize(address: str) -> str:
    return address.strip().replace("-", ":").upper()


class WalkingPadConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for WalkingPad."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_address: str | None = None
        self._discovered_name: str | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle a Bluetooth-triggered discovery."""
        address = _normalize(discovery_info.address)
        await self.async_set_unique_id(address)
        self._abort_if_unique_id_configured()

        self._discovered_address = address
        self._discovered_name = discovery_info.name or DEFAULT_NAME
        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm setup of a discovered device."""
        assert self._discovered_address is not None
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovered_name or DEFAULT_NAME,
                data={CONF_ADDRESS: self._discovered_address},
            )
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._discovered_name or DEFAULT_NAME,
                "address": self._discovered_address,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle manual setup."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = _normalize(user_input[CONF_ADDRESS])
            if not MAC_RE.match(address):
                errors["base"] = "invalid_address"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=DEFAULT_NAME,
                    data={CONF_ADDRESS: address},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): str}),
            errors=errors,
        )
