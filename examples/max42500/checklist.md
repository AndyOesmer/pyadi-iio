# MAX42500: Blue Pill GUI → no-OS IIO port checklist

Analysis of `MAX42500_Bluepill` (STM32 Blue Pill firmware + Tkinter GUI) and what
is required to rebuild the **same working concept** against the **MAX42500 no-OS
IIO driver** running on the MAX32690, accessed from Python through **pyadi-iio /
libiio**.

- Source analysed: `C:\Users\aoesmer\Downloads\MAX42500_Bluepill`
  (latest GUI `gui/max42500_monitor_gui_v1.8.py`, firmware `src/main.cpp` +
  `include/max42500.hpp`)
- Target firmware: `no-OS/drivers/power/max42500` (IIO device name `max42500`)
- Target transport: `adi.max42500(uri=...)` e.g. `serial:COM4,115200,8n1n` or `ip:<addr>`
  (baud is **115200** on both the AD-APARD32690-SL and the MAX32655FTHR; if 115200
  fails on the MAX32655FTHR, try **57600**)

---

## 1. What the Blue Pill project does (the concept to reproduce)

A desktop monitor/configurator for the MAX42500 7-input power-system supervisor:

1. **Connect** to the board over USB-serial and show live link/boot health.
2. **Per-channel table (IN1–IN7)**: pick a **Mode** (Enable / Monitor / Disable),
   set **OV** and **UV** thresholds, show live **OV / UV / OFF / Fault** LEDs.
3. **Device panel**: device ID + raw status registers + RESET / enable-pin state.
4. **Log** of all traffic, with "show data" / "show heartbeat" filters.
5. **Persist** settings to a JSON config next to the script; reload on startup;
   only push to the chip on **Force Set** or (with **Auto Set**) on edit.
6. Publish a small **bridge JSON** for an external consumer (OpenArm Studio).

---

## 2. How the Blue Pill talks to the chip (transport being replaced)

Custom ASCII line protocol over pyserial (115200):

| Direction | Line / command | Meaning |
|---|---|---|
| RX | `DATA,<ID>,<STATOV>,<STATUV>,<STATOFF>,<WDSTAT>,<CONFIG2>,<VMON>` | polled status (hex) |
| RX | `DBG,<STAGE>,OK\|FAIL,<detail>` | boot/health checkpoints |
| RX | `CFG,<CONFIG1>,<VMON>,<RSTMAP>,<0x08..0x15>` | reply to `C` |
| TX | `P <hz>` | poll rate 1..200 Hz |
| TX | `W <reg> <val>` | raw register write (VMON + 0x08–0x15 only) |
| TX | `E <mask>` | VMON channel-enable mask (bit0=IN1) |
| TX | `M <mask>` | RSTMAP reset-mapping mask (bit0=IN1) |
| TX | `C` | dump config/thresholds |

**All threshold math lives in the Python GUI**: it takes rail OV/UV volts + a
per-channel **V-Divider** ratio, computes pin volts, then solves for the register
codes (nominal set-point + OV/UV nibble codes, or absolute 5 mV codes for IN6/IN7)
and writes raw registers. The firmware is a thin I²C bridge (+ SMBus PEC).

**This whole layer is replaced by libiio.** The no-OS IIO driver exposes
engineering-unit attributes and does the register encoding itself.

---

## 3. no-OS IIO interface available on the target

Device name: **`max42500`**. Channels `voltage0..voltage6` = **IN1..IN7**.

### Per-channel attributes
| Attribute | IN1–IN5 (voltage0..4) | IN6–IN7 (voltage5..6) | Access | Units |
|---|---|---|---|---|
| `ov_status` / `uv_status` / `off_status` | ✓ | ✓ | R | 0/1 |
| `power_up_timestamp` / `power_down_timestamp` | ✓ | ✓ | R | µs |
| `nominal_voltage` (+`_available`) | ✓ | — | R/W | volts |
| `ov_threshold` (+`_available`) | ✓ (percent) | ✓ (volts) | R/W | % / V |
| `uv_threshold` (+`_available`) | ✓ (percent) | ✓ (volts) | R/W | % / V |

`*_available` are `"[min step max]"` strings, e.g.
`nominal_voltage` IN1–4 `[0.5 0.0125 3.6875]`, IN5 `[0.5 0.02 5.6]`;
`ov/uv_threshold` IN1–5 `[2.5 0.5 10.0]` (percent), IN6–7 `[0.5 0.005 1.775]` (volts).

### Device-global attributes
| Attribute | Access | Values |
|---|---|---|
| `device_state` (+`_available`) | R/W | `off` / `sleep` / `on` (EN0/EN1 state machine) |
| `watchdog_enable` | R/W | 0/1 |
| `watchdog_key` | R/W | read current key; **any write = refresh key** |
| `watchdog_reset_hold` (+`_available`) | R/W | `0ms` / `8ms` / `16ms` / `32ms` |

### Raw register access (fills every gap below)
`dev.reg_read(addr)` / `dev.reg_write(addr, val)` cover the full 0x00–0x2D map,
e.g. ID `0x00`, CONFIG2 `0x02`, VMON `0x03`, RSTMAP `0x04`,
STATOV/UV/OFF `0x05/06/07`, WDSTAT `0x26`.

---

## 4. Feature-by-feature port checklist

Legend: ✅ native IIO attribute · 🟡 via raw `reg_read`/`reg_write` · 🔵 GUI-side only

| Blue Pill feature | Port to no-OS IIO | How |
|---|---|---|
| Connect / Port select | **URI connect** (`serial:COMx,115200,8n1n` / `ip:`) | 🔵 `adi.max42500(uri=...)` in a worker thread |
| Boot/health "checkpoints" (DBG) | **Connection stages** | 🔵 context created → device found → ID read → attrs read → polling (firmware emits no DBG lines) |
| Poll rate `P <hz>` | **GUI poll cadence** | 🔵 worker loop sleeps `1/hz`; IIO has no poll-rate attribute |
| DATA status poll | per-channel `ov/uv/off_status` | ✅ read each monitored channel each tick |
| Device ID | `reg_read(0x00)` | 🟡 (silicon ID `0x30`) |
| STATOV/STATUV/STATOFF regs | `reg_read(0x05/0x06/0x07)` | 🟡 for the raw device panel (per-bit also ✅ via `*_status`) |
| OV limit (rail volts) + V-Divider | `ov_threshold` | ✅ GUI solves rail+divider → **percent** for IN1–5, **volts** for IN6–7 (driver encodes) |
| UV limit | `uv_threshold` | ✅ same unit rules |
| Nominal set-point (solved from rail+divider) | `nominal_voltage` | ✅ IN1–5 only, in volts |
| V-Divider / rail-volt scaling | **primary input model (ported from Blue Pill)** | 🔵 GUI solves register codes, then writes the engineering units; identical register result |
| Mode Enable/Monitor/Disable | VMON `0x03` + RSTMAP `0x04` | 🟡 `reg_write` masks (bit0=IN1); **not** IIO attributes |
| Fault LED / Active-fault count | derived from `ov_status\|uv_status` | 🔵 same rule as Blue Pill (count Enable only) |
| CONFIG2 RESET / RSTF / EN0 / EN1 | `reg_read(0x02)` display | 🟡 read-only panel (device_state write **disabled** to match old) |
| Watchdog (no-OS adds it; Blue Pill had none) | `watchdog_enable` / `watchdog_key` / `watchdog_reset_hold` | 🔵 panel shown but **disabled (read-only)**; only WDSTAT is read, matching the old firmware |
| `WDSTAT` | `reg_read(0x26)` | 🟡 raw only (no attribute) |
| Read Thresholds (`C`) → compare chip vs GUI | read all attrs + regs, diff | ✅/🟡 |
| Force Set / Auto Set | write attrs + mode regs | ✅/🟡 |
| Config JSON persist/reload | unchanged | 🔵 keep, add `uri` field |
| OpenArm bridge JSON | **ported, optional + configurable path** | 🔵 see §5.1 |
| Log + show-data/heartbeat filters | keep (no heartbeat line → repurpose/hide) | 🔵 |

---

## 5. Gaps & deltas to be explicit about

**Only reachable via raw `reg_read`/`reg_write` (no named attribute):**
- Whole-register STATOV/STATUV/STATOFF/WDSTAT/CONFIG1/CONFIG2.
- Per-channel **VMON enable** (0x03) and **RSTMAP** (0x04) → needed for the
  Enable/Monitor/Disable mode feature, so the GUI keeps a thin register layer.
- Poll-rate, PEC, FPS clock divider, full watchdog window config → init-time /
  raw only.

**New capability vs Blue Pill (worth surfacing in the GUI):**
- Thresholds/nominal read back in **engineering units** with valid-range strings.
- `power_up_timestamp` / `power_down_timestamp` in **µs**.
- `device_state` clean enum, and a real **watchdog** control group.

**Operator experience (kept identical to Blue Pill):**
- Enter **rail volts + V-Divider** per channel, exactly as on the Blue Pill.
- The GUI solves the register codes, then writes them to the driver as
  engineering units (`nominal_voltage` in V; `ov/uv_threshold` as percent for
  IN1–5, volts for IN6–7). The driver re-encodes to the same register codes, so
  the chip result is identical for the same rail + divider. Old
  `max42500_gui_config.json` files (with `divider`/`ov`/`uv`) load directly.
- `device_state` and the watchdog controls are shown but **read-only**, since the
  old firmware only read WDSTAT and the CONFIG2 EN0/EN1 pins.

### 5.1 OpenArm bridge (ported)

The Blue Pill GUI published the live power/fault state to a fixed JSON file
(`C:\OpenArmBridge\system_state.json`) via an atomic temp-file + `os.replace`
write, so an external consumer (OpenArm Studio) could react to a power fault.
That logic is **transport-agnostic** (it only consumes the decoded fault state),
so it is ported into the IIO GUI with these changes:

- **Optional and off by default** — the GUI runs normally whether or not the
  target path exists.
- **Configurable location** — set it three ways:
  - config key `bridge_path` (+ `bridge_enabled`) in `max42500_gui_config.json`,
  - the **OpenArm bridge** panel in the GUI (path entry + Browse + enable tick),
  - the command line: `python max42500_monitor_gui.py --bridge <path>`
    (also enables it; an optional first positional arg sets the URI).
- **Non-fatal** — a missing directory is created if possible; any write error is
  logged once and surfaced on the panel colour, but never raised.
- **Same JSON schema** (`power_fault`, `active_faults`, `fault_channels`,
  per-channel `channels{...}`, `device_id`, …) so an existing OpenArm consumer
  needs no change; `source` is `"MAX42500 Monitor (IIO)"`.

---

## 6. As-built — concrete changes from the old C++ Blue Pill

What the committed GUI (`max42500_gui/max42500_monitor_gui.py`) actually does
differently from the old `MAX42500_Bluepill` project (STM32 firmware `main.cpp` +
`gui/max42500_monitor_gui_v1.8.py`). Everything below is implemented in the code,
not just planned.

### Transport & firmware
| Old C++ Blue Pill | New IIO GUI |
|---|---|
| STM32 Blue Pill firmware; thin I²C + SMBus-PEC bridge | No bespoke firmware — talks to the MAX42500 **no-OS IIO driver** via pyadi-iio/libiio |
| Custom ASCII line protocol (`DATA/DBG/CFG` RX, `P/W/E/M/C` TX) over pyserial, fixed 115200 | **Transport-agnostic** libiio URIs: `serial:COMx,115200,8n1n`, `ip:`, `usb:`. 115200 baud on both AD-APARD32690-SL and MAX32655FTHR (MAX32655FTHR falls back to 57600 if 115200 fails) |
| All register-code math in the Python GUI; writes raw registers | GUI keeps the *same* rail-volt + V-Divider solver, but writes **engineering-unit attributes** (`nominal_voltage` V; `ov/uv_threshold` as % for IN1–5, V for IN6–7); the driver re-encodes. Same chip result for the same rail + divider |

### Behaviour that changed
- **Validation against the chip's own ranges** — the worker reads each channel's
  `nominal_voltage_available` / `ov_threshold_available` / `uv_threshold_available`
  `[min step max]` strings and the solver validates edits against them (old GUI
  used hard-coded limits).
- **Status polling is host-driven** — no firmware `P <hz>`; the worker loop sets
  the cadence and reads `ID, STATOV, STATUV, STATOFF, WDSTAT, CONFIG2, VMON` as
  whole registers via `reg_read` each tick (7 reads/tick), deriving the per-channel
  bits — mirroring the old `DATA` line without the firmware poll-rate command.
- **Channel modes via raw `reg_write`** — Enable/Monitor/Disable write VMON (0x03)
  and RSTMAP (0x04) directly (bit0=IN1, top bit preserved) instead of the old
  `E`/`M` mask commands; RESET is **un-mapped before enables** so an Enable→Monitor
  move cannot momentarily trip RESET.
- **Connection stages replace DBG checkpoints** — the firmware emits no `DBG`
  lines, so the GUI synthesises stage indicators: `CTX → DEV → ID → ATTR → POLL`.
- **Single-threaded libiio access** — all device I/O runs on one `IIOWorker`
  thread; the GUI exchanges commands/events over queues (libiio contexts are not
  thread-safe). The old GUI talked to pyserial directly.

### New capability surfaced (absent in the old project)
- Engineering-unit threshold/nominal **read-back** with valid-range strings.
- `device_state` enum (`off`/`sleep`/`on`) and a real **watchdog** group
  (`watchdog_enable`, `watchdog_reset_hold`, refresh-key) — shown but **read-only**
  to match the old firmware, which only read WDSTAT and the CONFIG2 EN0/EN1 pins.
- `power_up_timestamp` / `power_down_timestamp` (µs) exposed by the driver.

### Kept identical (ported, not changed)
- Dark theme & layout, IN1–IN7 table, OV/UV/OFF/Fault LEDs + active-fault count
  (Enable channels only), rail-volt + V-Divider operator model.
- Config JSON persist/reload — **adds a `uri` field**; old
  `max42500_gui_config.json` files (with `divider`/`ov`/`uv`) load directly.
- OpenArm bridge JSON — same schema (`power_fault`, `active_faults`,
  `fault_channels`, per-channel `channels{...}`, `device_id`, …), now **optional,
  off by default, configurable path** (config key / GUI panel / `--bridge`),
  non-fatal on write errors; `source` is `"MAX42500 Monitor (IIO)"`.
- Log with a *Show Data* filter (the old *show heartbeat* filter is dropped — the
  driver emits no heartbeat line).

---

## 7. Build acceptance checklist (for `max42500_gui/`)

- [ ] Connects via URI in a worker thread; all libiio access on that one thread.
- [ ] Connection-stage indicators (context / device / ID / attrs / polling).
- [ ] IN1–IN7 table: Mode, V-Divider, OV/UV (rail volts), solved pin readout.
- [ ] Live OV/UV/OFF/Fault LEDs + Active-fault counter (Enable channels counted).
- [ ] Validate edits via the solver (detection limits); colour bad fields.
- [ ] Force Set / Auto Set solve rail+divider and write nominal+OV+UV attrs + VMON/RSTMAP regs.
- [ ] Read-back ("Read Thresholds") compares chip vs GUI and reports diffs.
- [ ] Device panel: ID, raw status regs, CONFIG2 RESET/EN pins; `device_state` shown read-only.
- [ ] Watchdog panel: enable, reset-hold, refresh-key — shown but disabled (read-only).
- [ ] Config JSON persist/reload (adds `uri`); same dark theme & layout.
- [ ] Log with show-data filter; graceful connect/disconnect/error handling.
- [ ] OpenArm bridge: optional, configurable path (config / GUI / `--bridge`),
      off by default, non-fatal if the location is missing, same JSON schema.
