# WalkingPad — Home Assistant integration

Custom integration for KingSmith **WalkingPad** treadmills. Connects over Bluetooth and exposes the treadmill's live speed plus cumulative steps, time, and distance as Home Assistant sensors.

## Features

- UI configuration (no YAML)
- Auto-discovery of WalkingPads via Home Assistant's Bluetooth integration
- Manual setup via Bluetooth MAC address
- Auto-connect when the treadmill is powered on
- Automatic reconnect after a disconnection
- Cumulative counters that survive pad pauses **and** Home Assistant restarts:
  - `sensor.<name>_speed` — instant speed (km/h)
  - `sensor.<name>_total_steps` — lifetime step count
  - `sensor.<name>_total_time` — lifetime walking time
  - `sensor.<name>_total_distance` — lifetime distance (km)

## Installation

### Via HACS (custom repository)

1. HACS → Integrations → ⋮ → Custom repositories
2. Add this repo's URL, category **Integration**
3. Install **WalkingPad**, then restart Home Assistant

### Manual

Copy `custom_components/walkingpad/` into your HA config's `custom_components/` directory and restart.

## Configuration

1. Power on the treadmill and make sure no phone is currently connected to it (the BLE radio only allows one client at a time).
2. In Home Assistant: Settings → Devices & Services → **Add Integration** → **WalkingPad**.
3. Either:
   - Accept the discovered device, or
   - Paste the Bluetooth MAC address manually (`AA:BB:CC:DD:EE:FF`).

## Notes

- The treadmill's session counters reset to zero whenever it's paused. The integration handles this by accumulating deltas into persistent totals — you'll never see your steps reset.
- Cumulative totals are stored in HA's `.storage/` directory, keyed by the config entry. Removing the integration deletes them.
- Compatible with Bluetooth proxies (ESPHome) — the integration uses Home Assistant's native Bluetooth API.

## Limitations

- Only one BLE client can be connected to the treadmill at a time. If you connect with the KS Fit phone app, Home Assistant will lose its connection until you disconnect the phone.
- The treadmill must be powered on for the sensors to update. Cumulative totals stay visible (frozen at the last known value) while the pad is off.
