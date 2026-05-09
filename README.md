# WalkingPad — Home Assistant integration

Custom integration for KingSmith **WalkingPad** treadmills. Connects over Bluetooth and exposes the treadmill's live speed plus cumulative, daily, and monthly counters as Home Assistant sensors.

## Features

- UI configuration (no YAML)
- Auto-discovery of WalkingPads via Home Assistant's Bluetooth integration
- Manual setup via Bluetooth MAC address
- Auto-connect when the treadmill is powered on
- Robust reconnection: bleak disconnect callback, status-notification watchdog, and a 30 s periodic reconnect probe (with HA BT cache rediscovery) — survives unclean disconnects and slot exhaustion
- Connection goes through `bleak_retry_connector.establish_connection`, so the BT slot manager is respected and BT proxies (ESPHome) are supported
- Counters survive treadmill pauses **and** Home Assistant restarts; deltas are stored on every notification

## Sensors

| Entity                            | Unit         | State class                              | Notes                                       |
| --------------------------------- | ------------ | ---------------------------------------- | ------------------------------------------- |
| `sensor.<name>_speed`             | km/h         | `measurement`                            | Live speed; unavailable when disconnected   |
| `sensor.<name>_total_steps`       | steps / pas  | `total_increasing`                       | Lifetime                                    |
| `sensor.<name>_total_time`        | duration     | `total_increasing`                       | Lifetime                                    |
| `sensor.<name>_total_distance`    | km           | `total_increasing`                       | Lifetime                                    |
| `sensor.<name>_daily_steps`       | steps / pas  | `total` (resets at local midnight)       |                                             |
| `sensor.<name>_daily_time`        | duration     | `total` (resets at local midnight)       |                                             |
| `sensor.<name>_daily_distance`    | km           | `total` (resets at local midnight)       |                                             |
| `sensor.<name>_monthly_steps`     | steps / pas  | `total` (resets on the 1st at midnight)  |                                             |
| `sensor.<name>_monthly_time`      | duration     | `total` (resets on the 1st at midnight)  |                                             |
| `sensor.<name>_monthly_distance`  | km           | `total` (resets on the 1st at midnight)  |                                             |

The `steps` unit is translated to `pas` when Home Assistant is set to French.

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

## Example Lovelace card

Built-in cards only, no custom-card dependency:

```yaml
type: vertical-stack
cards:
  - type: gauge
    entity: sensor.walkingpad_speed
    min: 0
    max: 6
    needle: true
  - type: grid
    columns: 3
    square: false
    cards:
      - type: tile
        entity: sensor.walkingpad_daily_steps
        vertical: true
      - type: tile
        entity: sensor.walkingpad_daily_distance
        vertical: true
      - type: tile
        entity: sensor.walkingpad_daily_time
        vertical: true
  - type: grid
    columns: 3
    square: false
    cards:
      - type: tile
        entity: sensor.walkingpad_total_steps
        vertical: true
      - type: tile
        entity: sensor.walkingpad_total_distance
        vertical: true
      - type: tile
        entity: sensor.walkingpad_total_time
        vertical: true
```

## Behavior notes

- **Pause vs. lifetime**: the treadmill resets its session counters to zero whenever it's paused. The integration handles this by accumulating deltas into persistent totals — you'll never see your lifetime counters reset.
- **Daily / monthly reset**: triggered by an `async_track_time_change` callback firing at local 00:00:00. If Home Assistant was off across midnight (or the 1st of the month), the catch-up logic in `_maybe_reset_periodic` zeros the relevant bucket on the next coordinator setup so you don't get phantom carry-over.
- **Persistence**: cumulative totals + daily/monthly counters + their `last_reset` timestamps are stored in HA's `.storage/` directory, keyed by the config entry. Removing the integration deletes them.
- **Connection lifecycle**: the integration listens for advertisements via HA's BT callback. On link-up it polls every 2 s. If notifications stop for 10 s while we believe we're connected (`STATUS_TIMEOUT_SECONDS` in `const.py`), we treat the link as dead and arm a periodic reconnect probe that runs every 30 s until the pad reappears.
- **BT proxies**: connections go through HA's slot-aware `establish_connection`, so ESPHome BT proxies work without any extra setup.

## Limitations

- Only one BLE client can be connected to the treadmill at a time. If you connect with the KS Fit phone app, Home Assistant will lose its connection until you disconnect the phone.
- The treadmill must be powered on for live speed to update. Cumulative / daily / monthly counters stay visible (frozen at the last known value) while the pad is off.
- Some KingSmith firmwares don't broadcast their `local_name` in the initial advertisement, so auto-discovery may miss them — manual MAC entry always works as a fallback.
