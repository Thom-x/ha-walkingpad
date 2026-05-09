# AGENT.md — context for AI assistants working on ha-walkingpad

This file orients an AI agent that's just been dropped into this repo. Read it before making non-trivial changes.

## What this repo is

A Home Assistant custom integration (HACS-installable) that talks to a KingSmith **WalkingPad** treadmill over Bluetooth LE and exposes its data as HA sensors. Two-tier persistence:

- **Lifetime totals** (steps / time / distance) — `state_class=total_increasing`, never reset
- **Daily / monthly counters** — `state_class=total`, reset at local midnight (and on the 1st)
- **Live speed** — `measurement`, unavailable when disconnected

There's also a separate `main.py` at the repo root (one level up from this folder) — a standalone test script used to verify protocol behavior. **It is not part of the integration**; don't merge concerns.

## Layout

```
ha-walkingpad/
├── custom_components/walkingpad/
│   ├── __init__.py          # config entry setup/unload
│   ├── manifest.json         # HACS metadata, BT discovery matchers, requirements
│   ├── config_flow.py        # UI: BT discovery + manual MAC entry
│   ├── const.py              # all magic numbers + protocol UUIDs
│   ├── coordinator.py        # the heart of the integration — connection lifecycle, accumulation, persistence
│   ├── sensor.py             # 10 sensor entities defined declaratively
│   ├── icons.json            # mdi icons per translation_key
│   ├── strings.json          # source-of-truth translation file (en)
│   └── translations/
│       ├── en.json
│       └── fr.json
├── hacs.json                 # HACS minimum HA version
├── README.md                 # user-facing documentation
└── AGENT.md                  # this file
```

## Key concepts

### Connection lifecycle (coordinator.py)

The coordinator is the only component that owns BLE state. Sensors are dumb projections of `coordinator.data`.

Flow:

1. **Setup** (`async_setup`): load persisted totals, register an HA Bluetooth callback on the address, seed initial coordinator data, and try to connect if the device is currently visible to HA's BT integration.
2. **Connect** (`_async_connect`): uses `bleak_retry_connector.establish_connection`, *not* `Controller.run()`. We need HA's slot manager to track the connection — `Controller.run()` creates a raw `BleakClient` that bypasses the slot allocator and causes `No backend with an available connection slot` errors after a disconnect cycle. After establishing, we manually find the `0xFE01` (notify) and `0xFE02` (write) characteristics and call `start_notify`. The Controller object is kept around purely for its `notif_handler`, `ask_stats()`, and command-throttling logic.
3. **Three independent disconnect detectors** — they overlap deliberately because none is reliable on its own:
   - bleak's `disconnected_callback` (passed to `establish_connection`) — fastest path, fires when BlueZ sees the link drop. Can be slow on Linux due to link supervision timeout (5–20 s).
   - Polling check on `client.is_connected` in `_async_update_data`.
   - **Watchdog** (`STATUS_TIMEOUT_SECONDS`): if no status notification for 10 s while we believe we're connected, treat the link as dead. This is the only reliable detector when BlueZ keeps `is_connected=True` past the actual link drop.
4. **Reconnect strategy** — also redundant on purpose:
   - HA Bluetooth callback fires on every advertisement matching the configured address → triggers `_async_connect`.
   - Periodic 30 s timer (`_unsub_reconnect_timer`) probes while disconnected, calls `async_rediscover_address` to nudge HA's BT cache, then attempts connection. This is the safety net for cases where BlueZ stops emitting advertisements for the address after a disconnect.
   - `RECONNECT_BACKOFF_SECONDS` (5 s) prevents hammering. Reset to 0 on disconnect so a fresh advertisement triggers an immediate retry.

### Accumulation logic

The pad reports session counters that reset to zero whenever it's paused. We turn those into monotonic counters via delta tracking:

```python
def _delta(current, last):
    if current < last:
        return current, current   # session reset detected — value is the new contribution
    return current - last, current  # normal increment
```

Each delta is added to **all three buckets** simultaneously (lifetime / daily / monthly). On midnight (or 1st of month), the relevant bucket is zeroed.

### Distance unit gotcha

The pad reports distance in **decameters** (10 m units, `1 = 0.01 km`), *not* centimeters. Internal variables are `*_dist_dam` and the conversion in `_build_data` is `dam / 100.0` to get km. Don't be tempted to call them `*_dist_cm` (an earlier version did, with a wrong divisor of `100_000`, producing values 1000× too small).

### Persistence

- `homeassistant.helpers.storage.Store` with key `walkingpad_totals_<entry_id>`.
- Saved on every status delta and at shutdown.
- `last_reset` timestamps (datetime) are serialized via `isoformat()`.
- Loaded eagerly in `async_setup`; `_maybe_reset_periodic` runs after load to catch up on any missed midnight/month boundaries.

### Protocol UUIDs

Defined in `const.py` as `WALKINGPAD_NOTIFY_UUID` and `WALKINGPAD_WRITE_UUID`. These are not user configuration — they're inherent to the WalkingPad firmware and identical across all KingSmith models. The `ph4_walkingpad` library hardcodes the same strings inside its `Controller.run()`. We extract them to constants for readability, not configurability.

## Conventions

- **No hardcoded device addresses anywhere in the code**. The MAC comes exclusively from `entry.data[CONF_ADDRESS]`.
- **Translation keys drive everything**: entity names, units, and icons are all looked up by `translation_key`. When adding a sensor, update *four* files: `sensor.py` (description), `strings.json`, `translations/en.json`, `translations/fr.json`, plus `icons.json` if a custom icon is needed.
- **Custom units must use translation, not `native_unit_of_measurement`**: HA refuses sensors that have both. The `steps`/`pas` unit is delivered via the entity translation file's `unit_of_measurement` key. SI units (km, s) come from device classes and are translated by HA core.
- **Logging levels**: state transitions and connection events at `INFO`. Per-tick / per-advertisement noise at `DEBUG`. Failures users may need to act on at `WARNING`.
- **No comments explaining "what" — only "why"**. The codebase is small enough that names should carry weight. Reserve comments for non-obvious invariants (e.g. why we have three disconnect detectors).
- **Don't introduce new Python deps without updating `manifest.json`'s `requirements`**. HA installs them automatically at integration load time.
- **Don't add YAML configuration paths**. The integration is UI-only by design (config flow). The user explicitly asked for this.

## Common tasks

### Adding a new sensor

1. Add the data field to `WalkingPadCoordinator.__init__` (and persistence if needed).
2. Update `_on_status` and `_build_data`.
3. Add a `WalkingPadSensorDescription` entry to `SENSORS` in `sensor.py`.
4. Add `translation_key` entries in `strings.json`, `translations/en.json`, `translations/fr.json`.
5. Optionally add an icon in `icons.json`.

### Bumping ph4_walkingpad

The library version is pinned in `manifest.json`. Before bumping, verify that `Controller.notif_handler`, `Controller.ask_stats`, and `Controller.disconnect` still exist with compatible signatures, since we use them directly without going through `Controller.run()`.

### Tweaking timing

All timing constants live in `const.py`:
- `POLL_INTERVAL_SECONDS` — how often we send `ask_stats` while connected
- `RECONNECT_BACKOFF_SECONDS` — minimum gap between connect attempts
- `STATUS_TIMEOUT_SECONDS` — watchdog: how long without a notification before declaring the link dead

The 30 s periodic reconnect probe is hardcoded in `_schedule_reconnect_timer`. Move to `const.py` if you need to make it configurable.

## Things not to do

- **Don't call `Controller.run()`**. It creates its own `BleakClient` outside HA's slot manager. Always use `establish_connection` and assemble the Controller manually (set `client`, find chars, `start_notify`).
- **Don't trust `client.is_connected` alone**. BlueZ can lie for up to 20 s after the peer disappears. The watchdog is essential.
- **Don't log advertisements at INFO**. Per-second noise floods the logs. Use INFO only for state transitions.
- **Don't swallow the `BleakClient` returned by `establish_connection`**. Assign it to `ctrl.client` so all downstream Controller methods (which write through `self.client`) work transparently.
- **Don't bypass `_connect_lock`**. Multiple paths can trigger `_async_connect` simultaneously (BT callback + periodic timer); the lock prevents racing connection attempts.

## Testing manually

There is no automated test suite. Manual flow:

1. Restart HA after copying / updating the integration files.
2. Settings → Devices & Services → Add Integration → WalkingPad.
3. With DEBUG logging on `custom_components.walkingpad`, exercise: pad on → walk → pause → resume → walk → power off → wait 30 s → power on → walk again → cross midnight (if patient).
4. Verify in *Developer Tools → States* that lifetime, daily, and monthly counters all increment correctly and that `last_reset` timestamps line up.

To enable DEBUG via UI: integration card → ⋮ → Enable debug logging.
