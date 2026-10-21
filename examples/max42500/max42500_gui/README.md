# MAX42500 Monitor — no-OS IIO edition

A dark-mode desktop monitor / configurator for the **MAX42500** 7-input
power-system supervisor, built on **pyadi-iio / libiio**.

Because it speaks to the **MAX42500 no-OS IIO driver** (not any board-specific
protocol), it is **platform-agnostic**. It runs against any target that exports
that IIO device — primarily Maxim MCU boards such as the **AD-APARD32690-SL
(MAX32690)** or **MAX32666** — over whatever transport libiio provides
(serial / network / USB).

> This is the IIO re-implementation of a reference serial monitor. The feature
> mapping (what became a native IIO attribute, what needs raw register access,
> and what is GUI-side only) is in
> [`../checklist.md`](../checklist.md).

## Requirements

- `pyadi-iio` (this repo) and a matching `libiio` / `pylibiio` install.
  On Windows, the bindings must match the installed libiio (e.g. `pylibiio==0.25`
  for a libiio 0.25 install).
- `pyserial` (used only to *suggest* `serial:` URIs in the Refresh button).
- Target firmware: the no-OS `max42500` **iio_example** application.

> New machine? See [`../SETUP.md`](../SETUP.md) for the full clean-room procedure
> (flash the firmware → install libiio → venv → build pyadi-iio → run), and
> [`../requirements.txt`](../requirements.txt) for the pinned Python deps.

## Run

```powershell
python max42500_monitor_gui.py
```

Enter the context **URI** and press **Connect**:

| Target / transport | URI example |
|---|---|
| Serial (tinyiiod over UART/USB-CDC) | `serial:COM4,115200,8n1n` |
| Network (iiod) | `ip:192.168.2.1` |
| USB | `usb:1.2.5` |

**Refresh** lists detected COM ports as ready-made `serial:` URIs; the URI field
is editable, so you can also type an `ip:`/`usb:` URI.

## What it shows

- **Connection** stages: IIO context → device found → ID register → thresholds
  read → status polling.
- **Channels (IN1–IN7)**: per-channel **Mode**, **V-Divider**, **OV/UV limit**
  (rail volts), a read-only **Scaled pin (V)** readout of the solved thresholds,
  and live **OV / UV / OFF / Fault** LEDs + an active-fault count.
- **Device**: ID and the raw `STATOV/STATUV/STATOFF/WDSTAT/CONFIG2/VMON`
  registers, plus the CONFIG2 **RESET / EN0 / EN1** pin state.
- **Watchdog / state**: `device_state` (`off`/`sleep`/`on`), `watchdog_enable`,
  `watchdog_reset_hold`, and a **Refresh key** action — all shown but **disabled
  (read-only)** to match the old firmware, which only read WDSTAT and the CONFIG2
  EN0/EN1 pins and never wrote device state or operated the watchdog.
- **Log** with a *Show Data* filter.

## Threshold model (V-Divider, same as the Blue Pill GUI)

Enter a per-channel **V-Divider** ratio (V_pin / V_rail, 1.0 = no divider) and
**OV/UV limits in rail volts**. The GUI scales to the pin and solves the values
the driver needs, so the register result matches the old firmware for the same
rail + divider:

| Channel | What the driver receives |
|---|---|
| IN1–IN5 | `nominal_voltage` (V) + `ov/uv_threshold` as **percent** of nominal |
| IN6–IN7 | `ov/uv_threshold` as absolute **volts** (no nominal) |

The solved pin thresholds (and nominal) are shown per row. Out-of-detection-range
or inconsistent values are highlighted and not sent.

## Channel modes

| Mode | VMON (0x03) | RSTMAP (0x04) | Behaviour |
|---|---|---|---|
| Enable | 1 | 1 | OV/UV monitored, faults mapped to RESET (full-colour LEDs, counted) |
| Monitor | 1 | 0 | OV/UV monitored/reported, RESET unaffected (dimmed LEDs) |
| Disable | 0 | 0 | monitors off (gray) |

Modes use raw `reg_write` on VMON/RSTMAP because the IIO driver does not expose
per-channel enable/mapping as named attributes. RESET un-mapping is sent before
enables so moving a channel Enable→Monitor cannot momentarily trip RESET.

## Buttons

- **Force Set** — solve every channel (divider + rail OV/UV) and push the
  resulting nominal/OV/UV values and the mode masks to the chip.
- **Read Thresholds** — read the chip back and report per-channel match / diffs.
- **Auto Set** — when on, edits are pushed as you make them.
- **Poll Rate (Hz)** — GUI-side polling cadence (IIO has no poll-rate attribute).

Settings persist to `max42500_gui_config.json` next to the script and reload on
startup; they are only written to the chip on Force Set / Auto Set.

## OpenArm bridge (optional)

The GUI can publish the live power/fault state to a JSON file (atomic
temp-file + replace) so an external program — e.g. OpenArm Studio — can react to
a power fault. It is **off by default** and the GUI runs fine whether or not the
target location exists.

Point it anywhere, three ways (any one works):

```powershell
# 1) command line (also enables it); optional first arg sets the URI
python max42500_monitor_gui.py --bridge C:\OpenArmBridge\system_state.json
python max42500_monitor_gui.py serial:COM4,115200,8n1n --bridge D:\shared\state.json

# 2) the "OpenArm bridge" panel in the GUI: tick "Publish state",
#    type/Browse to a path.

# 3) max42500_gui_config.json:
#    "bridge_enabled": true,
#    "bridge_path": "C:/OpenArmBridge/system_state.json"
```

Behaviour: a missing directory is created if possible; any write error is logged
once and shown by the panel colour (green ok / amber stale or error / red power
fault), but **never crashes the GUI**. The JSON schema matches the original
monitor (`power_fault`, `active_faults`, `fault_channels`, per-channel
`channels{...}`, `device_id`, …) so an existing consumer needs no change; the
`source` field is `"MAX42500 Monitor (IIO)"`. On exit a final record is written
with `gui_running: false` and faults cleared.

## Design notes

- All libiio access runs on a single worker thread (libiio contexts are not
  safe to share across threads); the GUI exchanges commands/events with it via
  queues.
- Status polling reads `STATOV/STATUV/STATOFF` as whole registers via
  `reg_read` (3 reads/tick) and derives the per-channel bits, mirroring the
  reference monitor's `DATA` line.
