# Copyright (C) 2026 Analog Devices, Inc.
#
# SPDX short identifier: ADIBSD
"""MAX42500 Monitor (no-OS IIO edition, dark mode, single file).

A desktop monitor / configurator for the MAX42500 7-input power-system
supervisor. It talks to the **MAX42500 no-OS IIO driver** through pyadi-iio /
libiio, so it is **platform-agnostic**: it works against any board running that
firmware -- primarily Maxim MCU targets such as the AD-APARD32690-SL (MAX32690)
or MAX32666 -- over whatever transport libiio exposes (serial, network, USB).

The concept (per-channel OV/UV monitoring, live status LEDs, threshold
configuration) is ported from an earlier reference monitor; the mapping of that
concept onto the IIO interface is documented in
``../checklist.md``.

Transport
    adi.max42500(uri=...)   e.g.  serial:COM4,115200,8n1n   or   ip:<addr>

Per-channel entry (V-Divider model, same as the Blue Pill GUI):
    You enter a V-Divider ratio (V_pin / V_rail) and OV/UV limits in RAIL volts.
    The GUI scales to the pin and solves the values the driver needs:
      IN1-IN5 : nominal_voltage (V) + ov_threshold / uv_threshold in PERCENT
      IN6-IN7 : ov_threshold / uv_threshold in VOLTS (no nominal)
    so the register result matches the old firmware for the same rail + divider.

Channel modes (written as raw VMON 0x03 / RSTMAP 0x04 masks, bit0 = IN1):
    Enable  - OV/UV monitored and mapped to RESET   (VMON=1, RSTMAP=1)
    Monitor - OV/UV monitored, not mapped to RESET   (VMON=1, RSTMAP=0)
    Disable - OV/UV monitors off                     (VMON=0, RSTMAP=0)

Settings are saved to max42500_gui_config.json next to this script and loaded on
startup, but never pushed to the chip unless you press Force Set, or Auto Set is
on and you change something.

Requires:  pyadi-iio and a matching libiio (pylibiio) install.
"""

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Optional

import serial.tools.list_ports  # only used to suggest serial: URIs

import adi

# =====================================================================
# Channel model
# =====================================================================

MODE_ENABLE = "Enable"
MODE_MONITOR = "Monitor"
MODE_DISABLE = "Disable"
MODES = [MODE_ENABLE, MODE_MONITOR, MODE_DISABLE]


# =====================================================================
# Threshold conversion (V-Divider model, ported from the Blue Pill GUI)
#
# V-Divider is the divider ratio V_pin / V_rail, i.e.
#     ratio = R_bottom / (R_top + R_bottom)        (1.0 = no divider)
# OV/UV are entered as RAIL volts; the pin threshold = rail * ratio.
#
# IN1-IN5: nominal voltage plus percentage thresholds (datasheet)
#   IN1-IN4 nominal = 0.5 V + 12.5 mV * code   (0.5 V to 3.6875 V)
#   IN5     nominal = 0.5 V + 20 mV   * code   (0.5 V to 5.5 V)
#   OV (%) = 102.5 % + 0.5 % * OV[3:0]   (102.5 % to 110 %)
#   UV (%) = 97.5 %  - 0.5 % * UV[3:0]   (97.5 % to 90 %)
# IN6/IN7: absolute thresholds V = 0.5 V + 5 mV * code (0.5 V to 1.775 V)
# All IN pins: absolute maximum 6 V.
#
# The GUI solves rail volts + divider into the nominal / percent / volt values
# the no-OS driver accepts (nominal_voltage, ov_threshold, uv_threshold), so the
# register result matches the old firmware for the same rail + divider.
# =====================================================================

PIN_ABS_MAX_V = 6.0
PCT_TOLERANCE = 0.005  # reject results more than 0.5 % from the request
OV_PCT_MIN, OV_PCT_MAX, OV_STEP = 1.025, 1.10, 0.005
UV_PCT_MIN, UV_PCT_MAX, UV_STEP = 0.90, 0.975, 0.005
NOM_MIN_V = 0.5
RATIO_MIN = OV_PCT_MIN / UV_PCT_MAX   # narrowest OV/UV window, about 1.051
RATIO_MAX = OV_PCT_MAX / UV_PCT_MIN   # widest OV/UV window, about 1.222
ABS_MIN_V, ABS_STEP_V, ABS_MAX_V = 0.5, 0.005, 1.775


@dataclass(frozen=True)
class ChannelSpec:
    name: str            # front-panel label IN1..IN7
    index: int           # IIO channel index -> voltage{index}, also mask bit
    kind: str            # "pct" (IN1-IN5) or "abs" (IN6-IN7)
    has_nominal: bool
    default_nominal: float    # rail nominal used to seed the default OV/UV limits
    nom_step: float = 0.0     # nominal-voltage code step (pct channels)
    nom_max: float = 0.0      # nominal-voltage maximum (pct channels)


CHANNEL_SPECS = [
    ChannelSpec("IN1", 0, "pct", True, 1.2, nom_step=0.0125, nom_max=3.6875),
    ChannelSpec("IN2", 1, "pct", True, 1.1, nom_step=0.0125, nom_max=3.6875),
    ChannelSpec("IN3", 2, "pct", True, 1.8, nom_step=0.0125, nom_max=3.6875),
    ChannelSpec("IN4", 3, "pct", True, 3.3, nom_step=0.0125, nom_max=3.6875),
    ChannelSpec("IN5", 4, "pct", True, 5.0, nom_step=0.02, nom_max=5.5),
    ChannelSpec("IN6", 5, "abs", False, 0.85),
    ChannelSpec("IN7", 6, "abs", False, 0.85),
]

DEFAULT_WINDOW = 0.05  # default OV/UV limits: nominal +/- 5 %

# Raw registers used for polling and the device panel (bit N = INn+1).
REG_ID = 0x00
REG_CONFIG2 = 0x02
REG_VMON = 0x03
REG_RSTMAP = 0x04
REG_STATOV = 0x05
REG_STATUV = 0x06
REG_STATOFF = 0x07
REG_WDSTAT = 0x26
SILICON_ID = 0x30

# CONFIG2 (0x02) bits: CLKF PAR RSTF RST EN1 EN0 BSTO BSTU
CFG2_RSTF, CFG2_RST, CFG2_EN1, CFG2_EN0 = 5, 4, 3, 2
RST_BIT_MEANS_PIN_HIGH = True


def default_limits(spec):
    """Default OV/UV rail limits: the channel's nominal +/- DEFAULT_WINDOW."""
    ov = spec.default_nominal * (1 + DEFAULT_WINDOW)
    uv = spec.default_nominal * (1 - DEFAULT_WINDOW)
    return f"{ov:.4g}", f"{uv:.4g}"


def default_channel(spec):
    ov, uv = default_limits(spec)
    # Divider defaults to 1.0 (no divider -> rail volts are the pin volts).
    return {"mode": MODE_DISABLE, "divider": "1.0", "ov": ov, "uv": uv}


def parse_float(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return float("nan")


def _is_number(value):
    return value is not None and value == value


def _clamp(value, low, high):
    return max(low, min(high, value))


@dataclass
class Result:
    status: str                         # "empty", "ok", or "error"
    message: str = ""                   # reason, logged when a send is skipped
    ratio: Optional[float] = None
    rail_ov: Optional[float] = None     # rail thresholds (after rounding when ok)
    rail_uv: Optional[float] = None
    rail_nom: Optional[float] = None
    ov_pin: Optional[float] = None      # thresholds at the chip pin
    uv_pin: Optional[float] = None
    nom_pin: Optional[float] = None     # IN1-IN5 only
    ov_bad: bool = False                # scaled OV outside what the channel detects
    uv_bad: bool = False                # scaled UV outside what the channel detects


def _solve_pct(spec, ov_pin, uv_pin):
    """Best register setting. Returns (ov_err, uv_err, d, oc, uc, ov, uv, nominal)."""
    best = None
    codes = int(round((spec.nom_max - NOM_MIN_V) / spec.nom_step))
    for d in range(min(codes, 255) + 1):
        nominal = NOM_MIN_V + spec.nom_step * d
        oc = _clamp(int(round((ov_pin / nominal - OV_PCT_MIN) / OV_STEP)), 0, 15)
        uc = _clamp(int(round((UV_PCT_MAX - uv_pin / nominal) / UV_STEP)), 0, 15)
        ov = nominal * (OV_PCT_MIN + OV_STEP * oc)
        uv = nominal * (UV_PCT_MAX - UV_STEP * uc)
        ov_err = abs(ov - ov_pin) / ov_pin
        uv_err = abs(uv - uv_pin) / uv_pin
        if best is None or max(ov_err, uv_err) < max(best[0], best[1]):
            best = (ov_err, uv_err, d, oc, uc, ov, uv, nominal)
    return best


def compute(spec, ratio_text, ov_text, uv_text):
    """Check a channel's rail-volt settings against the detection limits.

    Every failed check marks the scaled OV and/or UV as bad instead of stopping,
    so the GUI can colour exactly the value(s) that are out of range. On success
    the pin thresholds (and nominal, for IN1-IN5) are returned so the caller can
    derive the engineering units the no-OS driver accepts."""
    ratio = parse_float(ratio_text)
    ov = parse_float(ov_text)
    uv = parse_float(uv_text)

    if ov is None and uv is None:
        return Result("empty", "no limits set")

    result = Result("error")
    reasons = []
    ratio_ok = _is_number(ratio) and 0 < ratio <= 1
    if ratio_ok:
        result.ratio = ratio
    else:
        reasons.append("V-Divider must be a ratio > 0 and <= 1")

    if _is_number(ov):
        result.rail_ov = ov
    else:
        result.ov_bad = True
        reasons.append("OV missing or not a number")
    if _is_number(uv):
        result.rail_uv = uv
    else:
        result.uv_bad = True
        reasons.append("UV missing or not a number")

    if not ratio_ok:
        result.ov_bad = result.uv_bad = True
    if result.rail_ov is None or result.rail_uv is None or not ratio_ok:
        result.message = "; ".join(reasons)
        return result

    ov_pin, uv_pin = ov * ratio, uv * ratio
    result.ov_pin, result.uv_pin = ov_pin, uv_pin

    if ov <= uv:
        result.ov_bad = result.uv_bad = True
        reasons.append("OV must be above UV")

    if spec.kind == "abs":
        ov_low, ov_high, uv_low, uv_high = ABS_MIN_V, ABS_MAX_V, ABS_MIN_V, ABS_MAX_V
    else:
        ov_low = NOM_MIN_V * OV_PCT_MIN
        ov_high = min(spec.nom_max * OV_PCT_MAX, PIN_ABS_MAX_V)
        uv_low = NOM_MIN_V * UV_PCT_MIN
        uv_high = spec.nom_max * UV_PCT_MAX
    if not ov_low <= ov_pin <= ov_high:
        result.ov_bad = True
        reasons.append(f"OV scaled {ov_pin:.3f} V outside {ov_low:.3f}-{ov_high:.3f} V")
    if not uv_low <= uv_pin <= uv_high:
        result.uv_bad = True
        reasons.append(f"UV scaled {uv_pin:.3f} V outside {uv_low:.3f}-{uv_high:.3f} V")

    if spec.kind == "pct" and ov > uv:
        window = ov / uv
        if not RATIO_MIN - 1e-9 <= window <= RATIO_MAX + 1e-9:
            result.ov_bad = result.uv_bad = True
            reasons.append(f"OV/UV window {window:.3f} outside {RATIO_MIN:.3f}-{RATIO_MAX:.3f}")

    if result.ov_bad or result.uv_bad:
        result.message = "; ".join(reasons)
        return result

    if spec.kind == "abs":
        ov_code = _clamp(int(round((ov_pin - ABS_MIN_V) / ABS_STEP_V)), 0, 255)
        uv_code = _clamp(int(round((uv_pin - ABS_MIN_V) / ABS_STEP_V)), 0, 255)
        if ov_code <= uv_code:
            result.ov_bad = result.uv_bad = True
            result.message = "OV and UV round to the same 5 mV step"
            return result
        result.ov_pin = ABS_MIN_V + ABS_STEP_V * ov_code
        result.uv_pin = ABS_MIN_V + ABS_STEP_V * uv_code
    else:
        ov_err, uv_err, d, oc, uc, ov_a, uv_a, nominal = _solve_pct(spec, ov_pin, uv_pin)
        if max(ov_err, uv_err) > PCT_TOLERANCE:
            result.ov_bad = ov_err > PCT_TOLERANCE
            result.uv_bad = uv_err > PCT_TOLERANCE
            result.message = (f"no register setting within {PCT_TOLERANCE * 100:.1f} % "
                              f"(closest {ov_a / ratio:.3f} / {uv_a / ratio:.3f} V)")
            return result
        result.ov_pin, result.uv_pin, result.nom_pin = ov_a, uv_a, nominal
        result.rail_nom = nominal / ratio

    result.status = "ok"
    result.rail_ov = result.ov_pin / ratio
    result.rail_uv = result.uv_pin / ratio
    return result


def parse_available(text):
    """Parse a '[min step max]' libiio range string -> (min, step, max)."""
    try:
        inner = text.strip().lstrip("[").rstrip("]")
        parts = [float(p) for p in inner.split()]
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
    except (AttributeError, ValueError):
        pass
    return None


# =====================================================================
# IIO worker thread.  ALL libiio access happens here; the GUI talks to it
# only through the command and event queues (libiio contexts are not safe
# to touch from more than one thread).
# =====================================================================

@dataclass
class ChipSnapshot:
    device_id: Optional[int] = None
    availables: dict = field(default_factory=dict)   # index -> {nom,ov,uv ranges}
    thresholds: dict = field(default_factory=dict)   # index -> {nominal,ov,uv}
    vmon: Optional[int] = None
    rstmap: Optional[int] = None


class IIOWorker(threading.Thread):
    """Owns the adi.max42500 device and polls it."""

    def __init__(self, uri, cmd_q, evt_q, stop_event, poll_hz):
        super().__init__(daemon=True)
        self.uri = uri
        self.cmd_q = cmd_q
        self.evt_q = evt_q
        self.stop_event = stop_event
        self.poll_interval = 1.0 / max(1, poll_hz)
        self.dev = None

    # -- helpers ------------------------------------------------------
    def _emit(self, *event):
        self.evt_q.put(event)

    def _stage(self, key, ok, detail):
        self._emit("stage", key, ok, detail)

    def _channel(self, index):
        return self.dev.channel[index]

    # -- connect + initial read --------------------------------------
    def _connect(self):
        self._stage("CTX", None, f"opening {self.uri}")
        self.dev = adi.max42500(uri=self.uri)
        self._stage("CTX", True, f"context open ({self.uri})")
        self._stage("DEV", True, "device 'max42500' found")
        snap = ChipSnapshot()
        try:
            snap.device_id = int(self.dev.reg_read(REG_ID))
            ok = (snap.device_id & 0xF0) == SILICON_ID
            self._stage("ID", ok, f"ID=0x{snap.device_id:02X} "
                        + ("(MAX42500)" if ok else "(unexpected)"))
        except Exception as exc:  # noqa: BLE001
            self._stage("ID", False, f"ID read failed: {exc}")
        self._read_snapshot(snap)
        self._stage("ATTR", True, "thresholds / ranges read")
        self._emit("connected", snap)

    def _read_snapshot(self, snap):
        for spec in CHANNEL_SPECS:
            ch = self._channel(spec.index)
            ranges, values = {}, {}
            try:
                if spec.has_nominal:
                    ranges["nominal"] = parse_available(ch.nominal_voltage_available)
                    values["nominal"] = float(ch.nominal_voltage)
                ranges["ov"] = parse_available(ch.ov_threshold_available)
                ranges["uv"] = parse_available(ch.uv_threshold_available)
                values["ov"] = float(ch.ov_threshold)
                values["uv"] = float(ch.uv_threshold)
            except Exception as exc:  # noqa: BLE001
                self._emit("error", f"{spec.name}: read failed ({exc})")
            snap.availables[spec.index] = ranges
            snap.thresholds[spec.index] = values
        try:
            snap.vmon = int(self.dev.reg_read(REG_VMON))
            snap.rstmap = int(self.dev.reg_read(REG_RSTMAP))
        except Exception as exc:  # noqa: BLE001
            self._emit("error", f"VMON/RSTMAP read failed ({exc})")

    # -- polling ------------------------------------------------------
    def _poll(self):
        regs = {}
        for name, addr in (("id", REG_ID), ("statov", REG_STATOV),
                           ("statuv", REG_STATUV), ("statoff", REG_STATOFF),
                           ("wdstat", REG_WDSTAT), ("config2", REG_CONFIG2),
                           ("vmon", REG_VMON)):
            regs[name] = int(self.dev.reg_read(addr))
        self._emit("data", regs)

    # -- command handlers --------------------------------------------
    def _write_thresholds(self, index, nominal, ov, uv, note):
        spec = CHANNEL_SPECS[index]
        ch = self._channel(index)
        try:
            if spec.has_nominal and nominal is not None:
                ch.nominal_voltage = nominal
            if ov is not None:
                ch.ov_threshold = ov
            if uv is not None:
                ch.uv_threshold = uv
            self._emit("write", spec.name, True, note)
        except Exception as exc:  # noqa: BLE001
            self._emit("write", spec.name, False, f"{note}: {exc}")

    def _write_modes(self, vmon_mask, rstmap_mask, note):
        try:
            # Unmap from RESET first so moving Enable->Monitor can't trip RESET.
            cur_rst = int(self.dev.reg_read(REG_RSTMAP))
            self.dev.reg_write(REG_RSTMAP, (cur_rst & 0x80) | (rstmap_mask & 0x7F))
            cur_vmon = int(self.dev.reg_read(REG_VMON))
            self.dev.reg_write(REG_VMON, (cur_vmon & 0x80) | (vmon_mask & 0x7F))
            self._emit("write", "MODES", True, note)
        except Exception as exc:  # noqa: BLE001
            self._emit("write", "MODES", False, f"{note}: {exc}")

    def _read_config(self):
        snap = ChipSnapshot()
        try:
            snap.device_id = int(self.dev.reg_read(REG_ID))
        except Exception:  # noqa: BLE001
            pass
        self._read_snapshot(snap)
        self._emit("config", snap)

    def _handle_command(self, cmd):
        op = cmd[0]
        if op == "poll_hz":
            self.poll_interval = 1.0 / max(1, cmd[1])
        elif op == "thresholds":
            _, index, nominal, ov, uv, note = cmd
            self._write_thresholds(index, nominal, ov, uv, note)
        elif op == "modes":
            _, vmon, rstmap, note = cmd
            self._write_modes(vmon, rstmap, note)
        elif op == "read_config":
            self._read_config()
        elif op == "device_state":
            try:
                self.dev.device_state = cmd[1]
                self._emit("write", "device_state", True, cmd[1])
            except Exception as exc:  # noqa: BLE001
                self._emit("write", "device_state", False, str(exc))
        elif op == "wd_enable":
            try:
                self.dev.watchdog_enable = int(cmd[1])
                self._emit("write", "watchdog_enable", True, str(cmd[1]))
            except Exception as exc:  # noqa: BLE001
                self._emit("write", "watchdog_enable", False, str(exc))
        elif op == "wd_hold":
            try:
                self.dev.watchdog_reset_hold = cmd[1]
                self._emit("write", "watchdog_reset_hold", True, cmd[1])
            except Exception as exc:  # noqa: BLE001
                self._emit("write", "watchdog_reset_hold", False, str(exc))
        elif op == "wd_refresh":
            try:
                self.dev.refresh_watchdog_key()
                self._emit("write", "watchdog_key", True, "refreshed")
            except Exception as exc:  # noqa: BLE001
                self._emit("write", "watchdog_key", False, str(exc))

    # -- main loop ----------------------------------------------------
    def run(self):
        try:
            self._connect()
        except Exception as exc:  # noqa: BLE001
            self._stage("CTX", False, f"connect failed: {exc}")
            self._emit("error", f"Connect failed: {exc}")
            self._emit("closed", "not connected")
            return
        self._emit("info", f"Connected to {self.uri}")
        last_poll = 0.0
        try:
            while not self.stop_event.is_set():
                try:
                    while True:
                        self._handle_command(self.cmd_q.get_nowait())
                except queue.Empty:
                    pass
                now = time.monotonic()
                if now - last_poll >= self.poll_interval:
                    last_poll = now
                    try:
                        self._poll()
                        self._stage("POLL", True,
                                    f"last update {time.strftime('%H:%M:%S')}")
                    except Exception as exc:  # noqa: BLE001
                        self._stage("POLL", False, f"poll failed: {exc}")
                        self._emit("error", f"Poll failed: {exc}")
                        break
                time.sleep(0.01)
        finally:
            try:
                if self.dev is not None:
                    self.dev = None
            finally:
                self._emit("closed", f"Closed {self.uri}")


# =====================================================================
# Config
# =====================================================================

CONFIG_PATH = Path(__file__).with_name("max42500_gui_config.json")
CONFIG_VERSION = 1
POLL_OPTIONS = [1, 2, 5, 10, 20, 50]
DEFAULT_URI = "serial:COM4,115200,8n1n"

# OpenArm bridge: optionally publish the live power state to a JSON file for an
# external consumer (e.g. OpenArm Studio). Disabled by default; the GUI runs
# fine whether or not the path exists.
DEFAULT_BRIDGE_PATH = r"C:\OpenArmBridge\system_state.json"
BRIDGE_SCHEMA = 1
BRIDGE_HEARTBEAT_S = 1.0
BRIDGE_MIN_INTERVAL_S = 0.05
DATA_FRESH_S = 2.0


def default_config():
    return {"version": CONFIG_VERSION, "uri": DEFAULT_URI, "poll_hz": 5,
            "auto_set": False, "show_data": True,
            "bridge_enabled": False, "bridge_path": DEFAULT_BRIDGE_PATH,
            "channels": [default_channel(s) for s in CHANNEL_SPECS]}


def load_config():
    config = default_config()
    try:
        stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config, "no config file yet, using defaults"
    except (OSError, ValueError) as exc:
        return config, f"could not read config ({exc}), using defaults"
    for key in ("uri", "poll_hz", "auto_set", "show_data",
                "bridge_enabled", "bridge_path"):
        if key in stored:
            config[key] = stored[key]
    if config["poll_hz"] not in POLL_OPTIONS:
        config["poll_hz"] = 5
    for i, channel in enumerate(stored.get("channels", [])[:len(CHANNEL_SPECS)]):
        target = config["channels"][i]
        for key in ("divider", "ov", "uv"):
            if key in channel and str(channel[key]).strip() != "":
                target[key] = channel[key]
        if channel.get("mode") in MODES:
            target["mode"] = channel["mode"]
    return config, f"loaded {CONFIG_PATH.name}"


# =====================================================================
# OpenArm bridge
#
# Optional one-way file IPC: publish the MAX42500 power/fault state to a JSON
# file so an external program (OpenArm Studio) can react to a power fault. The
# whole document is written via a temp file + os.replace so a reader never sees
# a half-written file. Everything here is best-effort: with no path set it does
# nothing, and any write error is reported but never raised, so the GUI keeps
# running whether or not the target location exists.
# =====================================================================

class BridgeWriter:
    def __init__(self, path=None):
        self.path = None
        self.tmp_path = None
        self.seq = 0
        self.last_payload = None
        self.last_write = 0.0
        self.pending = False
        self.error = None
        self.set_path(path)

    def set_path(self, path):
        self.path = Path(path) if path else None
        self.tmp_path = (self.path.with_name(self.path.name + ".tmp")
                         if self.path else None)
        # A new (or cleared) path should write promptly on the next publish.
        self.last_payload = None
        self.pending = self.path is not None
        self.error = None

    def publish(self, state, force=False):
        if self.path is None:
            return False
        now = time.time()
        changed = state != self.last_payload
        due = now - self.last_write >= BRIDGE_HEARTBEAT_S
        if not (changed or due or force or self.pending):
            return False
        if not force and now - self.last_write < BRIDGE_MIN_INTERVAL_S:
            self.pending = self.pending or changed
            return False
        self.seq += 1
        document = {
            "schema": BRIDGE_SCHEMA, "source": "MAX42500 Monitor (IIO)",
            "seq": self.seq, "updated": round(now, 3),
            "updated_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        }
        document.update(state)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.tmp_path, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(self.tmp_path, self.path)
        except OSError as exc:
            self.pending = True
            self.error = str(exc)
            return False
        self.last_payload = state
        self.last_write = now
        self.pending = False
        self.error = None
        return True


# =====================================================================
# Theme (flat dark, matching the Blue Pill GUI)
# =====================================================================

BG = "#1E1E1E"
FIELD = "#3C3C3C"
FIELD_HOVER = "#474747"
BORDER = "#3F3F46"
FG = "#D4D4D4"
FG_BRIGHT = "#FFFFFF"
MUTED = "#8A9396"
ACCENT = "#0E639C"
ACCENT_HOVER = "#1177BB"
ACCENT_PRESSED = "#0B4F7D"
DISABLED_BG = "#2D2D30"
DISABLED_FG = "#6B6F73"

GREEN = "#2ECC71"
RED = "#E74C3C"
AMBER = "#F39C12"
NO_DATA = "#3A3D40"
INACTIVE = "#4B5054"
FAINT = {GREEN: "#1F6E43", RED: "#8A3B33", AMBER: "#8A6224", INACTIVE: "#383C3F"}
MODE_COLORS = {MODE_ENABLE: GREEN, MODE_MONITOR: AMBER, MODE_DISABLE: MUTED}
VALUE_WIDTH = 7
LED_SIZE = 14
LED_SUPERSAMPLE = 4

STAGES = [
    ("CTX", "IIO context"),
    ("DEV", "Device found"),
    ("ID", "ID register"),
    ("ATTR", "Thresholds read"),
    ("POLL", "Status polling"),
]


def _hex_to_rgb(color):
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


class LedImages:
    """Anti-aliased LED circles, one coverage mask shared by every colour."""

    def __init__(self, background=BG, size=LED_SIZE):
        self.background = _hex_to_rgb(background)
        self.size = size
        self.cache = {}
        self._alpha = self._build_alpha_mask(size, LED_SUPERSAMPLE)

    @staticmethod
    def _build_alpha_mask(size, samples):
        radius = size / 2.0 - 0.5
        center = size / 2.0
        mask = []
        for y in range(size):
            row = []
            for x in range(size):
                inside = sum(
                    1
                    for sy in range(samples)
                    for sx in range(samples)
                    if (x + (sx + 0.5) / samples - center) ** 2
                    + (y + (sy + 0.5) / samples - center) ** 2 <= radius * radius
                )
                row.append(inside / (samples * samples))
            mask.append(row)
        return mask

    def get(self, color):
        image = self.cache.get(color)
        if image is None:
            image = self._render(color)
            self.cache[color] = image
        return image

    def _render(self, color):
        fg, bg = _hex_to_rgb(color), self.background
        rows = []
        for row in self._alpha:
            pixels = []
            for a in row:
                r, g, b = (round(fg[i] * a + bg[i] * (1 - a)) for i in range(3))
                pixels.append(f"#{r:02x}{g:02x}{b:02x}")
            rows.append("{" + " ".join(pixels) + "}")
        image = tk.PhotoImage(width=self.size, height=self.size)
        image.put(" ".join(rows))
        return image


def apply_dark_theme(root):
    root.configure(bg=BG)
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", FG_BRIGHT)
    root.option_add("*TCombobox*Listbox.borderWidth", 0)

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=FG, fieldbackground=FIELD,
                    bordercolor=BORDER, lightcolor=BG, darkcolor=BG,
                    troughcolor=BG, focuscolor=BG, selectbackground=ACCENT,
                    selectforeground=FG_BRIGHT, insertcolor=FG)
    style.map(".", foreground=[("disabled", DISABLED_FG)])
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("TLabelframe", background=BG, bordercolor=BORDER,
                    lightcolor=BG, darkcolor=BG, relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=BG, foreground=FG_BRIGHT,
                    font=("Segoe UI", 9, "bold"))
    style.configure("TSeparator", background=BORDER)
    style.configure("TButton", background=ACCENT, foreground=FG_BRIGHT, bordercolor=ACCENT,
                    lightcolor=ACCENT, darkcolor=ACCENT, focuscolor=ACCENT, padding=(10, 3))
    button_states = [("disabled", DISABLED_BG), ("pressed", ACCENT_PRESSED),
                     ("active", ACCENT_HOVER)]
    style.map("TButton", background=button_states, bordercolor=button_states,
              lightcolor=button_states, darkcolor=button_states,
              foreground=[("disabled", DISABLED_FG)])
    style.configure("TCheckbutton", background=BG, foreground=FG,
                    indicatorbackground=FIELD, indicatorforeground=FG_BRIGHT,
                    lightcolor=BG, darkcolor=BG, focuscolor=BG)
    style.map("TCheckbutton", background=[("active", BG)],
              indicatorbackground=[("selected", ACCENT), ("active", FIELD_HOVER)])
    style.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG,
                    bordercolor=BORDER, lightcolor=FIELD, darkcolor=FIELD, padding=2)
    style.map("TEntry", bordercolor=[("focus", ACCENT)],
              lightcolor=[("focus", FIELD)], darkcolor=[("focus", FIELD)])
    style.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=FG,
                    arrowcolor=FG, bordercolor=BORDER, lightcolor=FIELD, darkcolor=FIELD,
                    padding=2)
    style.map("TCombobox",
              fieldbackground=[("readonly", FIELD), ("disabled", DISABLED_BG)],
              background=[("active", FIELD_HOVER), ("readonly", FIELD)],
              foreground=[("disabled", DISABLED_FG), ("readonly", FG)],
              selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)],
              bordercolor=[("focus", ACCENT)],
              lightcolor=[("focus", FIELD)], darkcolor=[("focus", FIELD)],
              arrowcolor=[("disabled", DISABLED_FG)])
    for mode, color in MODE_COLORS.items():
        name = f"{mode}.TCombobox"
        style.configure(name, foreground=color)
        style.map(name, foreground=[("readonly", color)],
                  selectforeground=[("readonly", color)])
    style.configure("Vertical.TScrollbar", background=FIELD, troughcolor=BG,
                    bordercolor=BG, arrowcolor=FG, lightcolor=FIELD, darkcolor=FIELD)
    style.map("Vertical.TScrollbar", background=[("active", FIELD_HOVER)],
              lightcolor=[("active", FIELD_HOVER)], darkcolor=[("active", FIELD_HOVER)])


def dark_title_bar(root):
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        for attribute in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:  # noqa: BLE001
        pass


# =====================================================================
# Per-channel row
# =====================================================================

class ChannelRow:
    def __init__(self, spec, settings):
        self.spec = spec
        self.mode = tk.StringVar(value=settings["mode"])
        self.divider = tk.StringVar(value=str(settings.get("divider", "1.0")))
        self.ov = tk.StringVar(value=str(settings["ov"]))
        self.uv = tk.StringVar(value=str(settings["uv"]))
        self.committed = self.field_text()
        self.ranges = {}       # nominal/ov/uv -> (min, step, max) from the chip
        self.name_label = None
        self.mode_box = None
        self.divider_entry = None
        self.ov_entry = None
        self.uv_entry = None
        self.calc_label = None
        self.leds = {}

    def field_text(self):
        return (self.divider.get().strip(), self.ov.get().strip(), self.uv.get().strip())

    def monitored(self):
        return self.mode.get() in (MODE_ENABLE, MODE_MONITOR)

    def mapped(self):
        return self.mode.get() == MODE_ENABLE

    def to_config(self):
        return {"mode": self.mode.get(), "divider": self.divider.get().strip(),
                "ov": self.ov.get().strip(), "uv": self.uv.get().strip()}


# =====================================================================
# Application
# =====================================================================

class App:
    def __init__(self, root, cli_uri=None, cli_bridge=None):
        self.root = root
        self.root.title("MAX42500 Monitor (IIO)")
        apply_dark_theme(root)
        self.led_images = LedImages()

        config, config_message = load_config()
        # Command-line overrides: [uri] and --bridge <path> (enables the bridge).
        if cli_uri:
            config["uri"] = cli_uri
        if cli_bridge is not None:
            config["bridge_path"] = cli_bridge
            config["bridge_enabled"] = True

        self.cmd_q = queue.Queue()
        self.evt_q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.connected = False
        self.last_status = None     # (statov, statuv, statoff)
        self.last_data_time = None
        self.reset_asserted = None
        self.device_id = None
        self.stage_widgets = {}
        # Bridge bookkeeping
        self.fault_events = 0
        self.last_fault_time = None
        self.bridge_prev_fault = False
        self.bridge_error_logged = None

        self.uri_var = tk.StringVar(value=config["uri"])
        self.poll_var = tk.StringVar(value=str(config["poll_hz"]))
        self.auto_set = tk.BooleanVar(value=bool(config["auto_set"]))
        self.show_data = tk.BooleanVar(value=bool(config["show_data"]))
        self.wd_enable = tk.BooleanVar(value=False)
        self.state_var = tk.StringVar(value="")
        self.hold_var = tk.StringVar(value="")
        self.bridge_enabled = tk.BooleanVar(value=bool(config["bridge_enabled"]))
        self.bridge_path_var = tk.StringVar(value=config["bridge_path"])
        self.bridge = BridgeWriter(
            config["bridge_path"] if config["bridge_enabled"] else None)
        self.rows = [ChannelRow(spec, config["channels"][i])
                     for i, spec in enumerate(CHANNEL_SPECS)]

        self._build()
        if self.bridge_enabled.get():
            self.bridge_label.config(
                text=f"-> {self.bridge_path_var.get().strip()}", foreground=MUTED)
        for i in range(len(self.rows)):
            self._validate_row(i)
        self._repaint_channels()
        self.refresh_ports()
        self._log(f"Config: {config_message} ({CONFIG_PATH})", "info")
        self._set_sync("Not connected", MUTED)
        dark_title_bar(self.root)
        self.root.update_idletasks()
        self.root.minsize(self.root.winfo_reqwidth(), self.root.winfo_reqheight())
        self.root.after(100, self._poll_events)

    # ------------------------------------------------------------- LEDs
    def _make_led(self, parent):
        label = tk.Label(parent, image=self.led_images.get(NO_DATA), bg=BG,
                         borderwidth=0, highlightthickness=0)
        label.led_color = NO_DATA
        return label

    def _paint(self, led, color):
        if getattr(led, "led_color", None) != color:
            led.led_color = color
            led.configure(image=self.led_images.get(color))

    # ------------------------------------------------------------- build
    def _build(self):
        top = ttk.LabelFrame(self.root, text="Connection")
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text="URI").grid(row=0, column=0, padx=(8, 2))
        self.uri_box = ttk.Combobox(top, textvariable=self.uri_var, width=28)
        self.uri_box.grid(row=0, column=1, padx=2, pady=8)
        ttk.Button(top, text="Refresh", command=self.refresh_ports).grid(row=0, column=2, padx=4)
        self.connect_button = ttk.Button(top, text="Connect", command=self.toggle_connection)
        self.connect_button.grid(row=0, column=3, padx=4)
        self.status_label = ttk.Label(top, text="Disconnected", foreground=RED, width=12)
        self.status_label.grid(row=0, column=4, padx=(4, 6))
        ttk.Label(top, text="Poll Rate (Hz)").grid(row=0, column=5, padx=(8, 2))
        self.poll_box = ttk.Combobox(top, textvariable=self.poll_var, state="readonly", width=5,
                                     values=[str(r) for r in POLL_OPTIONS])
        self.poll_box.grid(row=0, column=6, padx=2)
        self.poll_box.bind("<<ComboboxSelected>>", lambda _e: self._on_poll_changed())
        ttk.Checkbutton(top, text="Auto Set", variable=self.auto_set,
                        command=self._schedule_save).grid(row=0, column=7, padx=(10, 4))
        self.force_button = ttk.Button(top, text="Force Set", state="disabled",
                                       command=self.force_set)
        self.force_button.grid(row=0, column=8, padx=4)
        self.read_button = ttk.Button(top, text="Read Thresholds", state="disabled",
                                      command=self.read_thresholds)
        self.read_button.grid(row=0, column=9, padx=4)
        self.sync_label = ttk.Label(top, text="", width=24)
        self.sync_label.grid(row=0, column=10, padx=(6, 8), sticky="w")

        # Channel table
        table = ttk.LabelFrame(self.root, text="Channels")
        table.pack(fill="x", padx=10, pady=4)
        headers = ((0, "Channel"), (1, "Mode"), (2, "V-Divider"),
                   (3, "OV limit (V)"), (4, "UV limit (V)"), (5, "Scaled pin (V)"))
        for col, text in headers:
            ttk.Label(table, text=text).grid(row=0, column=col, rowspan=2, padx=5, pady=2,
                                             sticky="s")
        ttk.Separator(table, orient="vertical").grid(
            row=0, column=6, rowspan=len(self.rows) + 2, sticky="ns", padx=6)
        ttk.Label(table, text="Indicators").grid(row=0, column=7, columnspan=4, pady=(2, 0))
        for col, text in ((7, "OV"), (8, "UV"), (9, "OFF"), (10, "Fault")):
            ttk.Label(table, text=text, foreground=MUTED, anchor="center").grid(
                row=1, column=col, padx=3, pady=(0, 2))

        for i, row in enumerate(self.rows, 2):
            index = i - 2
            row.name_label = ttk.Label(table, text=row.spec.name)
            row.name_label.grid(row=i, column=0, padx=(6, 3))
            row.mode_box = ttk.Combobox(table, textvariable=row.mode, values=MODES,
                                        state="readonly", width=8)
            row.mode_box.grid(row=i, column=1, padx=3, pady=1)
            row.mode_box.bind("<<ComboboxSelected>>", lambda _e: self._on_mode_changed())
            # V-Divider applies to every channel (bit0 = IN1).
            row.divider_entry = ttk.Entry(table, textvariable=row.divider, width=8,
                                          justify="right")
            row.divider_entry.grid(row=i, column=2, padx=3, pady=1)
            row.divider_entry.bind("<Return>", lambda _e, n=index: self._on_commit(n))
            row.divider_entry.bind("<FocusOut>", lambda _e, n=index: self._on_commit(n))
            row.ov_entry = ttk.Entry(table, textvariable=row.ov, width=8, justify="right")
            row.ov_entry.grid(row=i, column=3, padx=3, pady=1)
            row.uv_entry = ttk.Entry(table, textvariable=row.uv, width=8, justify="right")
            row.uv_entry.grid(row=i, column=4, padx=3, pady=1)
            for entry in (row.ov_entry, row.uv_entry):
                entry.bind("<Return>", lambda _e, n=index: self._on_commit(n))
                entry.bind("<FocusOut>", lambda _e, n=index: self._on_commit(n))
            # Read-only readout of the solved pin thresholds (and nominal).
            row.calc_label = ttk.Label(table, text="—", foreground=MUTED,
                                       width=22, anchor="w")
            row.calc_label.grid(row=i, column=5, padx=(4, 8), sticky="w")
            for col, key in ((7, "OV"), (8, "UV"), (9, "OFF"), (10, "FAULT")):
                led = self._make_led(table)
                led.grid(row=i, column=col, padx=4, pady=2)
                row.leds[key] = led

        self.fault_counter = tk.Label(table, text="Active Fault: --", bg=BG, fg=MUTED,
                                      font=("Segoe UI", 10, "bold"))
        self.fault_counter.grid(row=len(self.rows) + 2, column=7, columnspan=4, pady=(4, 6))

        # Middle: connection stages + device + watchdog
        middle = ttk.Frame(self.root)
        middle.pack(fill="x", padx=10, pady=4)
        checks = ttk.LabelFrame(middle, text="Connection")
        checks.pack(side="left", fill="both", expand=True, padx=(0, 5))
        for r, (key, title) in enumerate(STAGES):
            led = self._make_led(checks)
            led.grid(row=r, column=0, padx=(8, 4), pady=1)
            ttk.Label(checks, text=title, width=16).grid(row=r, column=1, sticky="w")
            detail = ttk.Label(checks, text="waiting", foreground=MUTED, width=40)
            detail.grid(row=r, column=2, sticky="w", padx=(4, 8))
            self.stage_widgets[key] = (led, detail)

        right = ttk.Frame(middle)
        right.pack(side="left", fill="both")
        device = ttk.LabelFrame(right, text="Device")
        device.pack(fill="x")
        self.id_label = ttk.Label(device, text="ID: --")
        self.id_label.pack(anchor="w", padx=8, pady=2)
        self.raw_label = ttk.Label(device, text="STATOV=--  STATUV=--  STATOFF=--\n"
                                                "WDSTAT=--  CONFIG2=--  VMON=--",
                                   font=("Consolas", 9))
        self.raw_label.pack(anchor="w", padx=8, pady=(0, 4))

        reset = ttk.LabelFrame(right, text="RESET / enable pins (CONFIG2)")
        reset.pack(fill="x", pady=(5, 0))
        self.rstf_led = self._make_led(reset)
        self.rstf_led.grid(row=0, column=0, padx=(8, 4), pady=2)
        ttk.Label(reset, text="Reset fault flag (RSTF)").grid(row=0, column=1, sticky="w")
        self.rstf_label = ttk.Label(reset, text="--", width=18)
        self.rstf_label.grid(row=0, column=2, sticky="w", padx=4)
        self.rst_pin_led = self._make_led(reset)
        self.rst_pin_led.grid(row=1, column=0, padx=(8, 4), pady=2)
        ttk.Label(reset, text="RESET pin readback").grid(row=1, column=1, sticky="w")
        self.rst_pin_label = ttk.Label(reset, text="--", width=18)
        self.rst_pin_label.grid(row=1, column=2, sticky="w", padx=4)
        self.en_label = ttk.Label(reset, text="EN0: --   EN1: --")
        self.en_label.grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 4))

        watchdog = ttk.LabelFrame(right, text="Watchdog / state")
        watchdog.pack(fill="x", pady=(5, 0))
        ttk.Label(watchdog, text="Device state").grid(row=0, column=0, sticky="w", padx=8)
        self.state_box = ttk.Combobox(watchdog, textvariable=self.state_var, state="disabled",
                                      width=8, values=["off", "sleep", "on"])
        self.state_box.grid(row=0, column=1, padx=4, pady=2)
        self.state_box.bind("<<ComboboxSelected>>",
                            lambda _e: self._send(("device_state", self.state_var.get())))
        self.wd_check = ttk.Checkbutton(watchdog, text="WD enable", variable=self.wd_enable,
                                        state="disabled", command=self._on_wd_enable)
        self.wd_check.grid(row=0, column=2, padx=8)
        ttk.Label(watchdog, text="Reset hold").grid(row=1, column=0, sticky="w", padx=8)
        self.hold_box = ttk.Combobox(watchdog, textvariable=self.hold_var, state="disabled",
                                     width=8, values=["0ms", "8ms", "16ms", "32ms"])
        self.hold_box.grid(row=1, column=1, padx=4, pady=2)
        self.hold_box.bind("<<ComboboxSelected>>",
                           lambda _e: self._send(("wd_hold", self.hold_var.get())))
        self.wd_refresh_btn = ttk.Button(watchdog, text="Refresh key", state="disabled",
                                         command=lambda: self._send(("wd_refresh",)))
        self.wd_refresh_btn.grid(row=1, column=2, padx=8, pady=2)

        # OpenArm bridge (optional external state publisher)
        bridge = ttk.LabelFrame(right, text="OpenArm bridge (system_state.json)")
        bridge.pack(fill="x", pady=(5, 0))
        ttk.Checkbutton(bridge, text="Publish state", variable=self.bridge_enabled,
                        command=self._on_bridge_toggle).grid(
            row=0, column=0, sticky="w", padx=8, pady=(4, 2))
        self.bridge_label = ttk.Label(bridge, text="(disabled)", foreground=MUTED,
                                      width=30)
        self.bridge_label.grid(row=0, column=1, columnspan=2, sticky="w", padx=4)
        path_entry = ttk.Entry(bridge, textvariable=self.bridge_path_var, width=32)
        path_entry.grid(row=1, column=0, columnspan=2, sticky="we", padx=(8, 4), pady=(0, 6))
        path_entry.bind("<Return>", lambda _e: self._on_bridge_path())
        path_entry.bind("<FocusOut>", lambda _e: self._on_bridge_path())
        ttk.Button(bridge, text="Browse", command=self._browse_bridge).grid(
            row=1, column=2, padx=(0, 8), pady=(0, 6))

        tk.Label(self.root, text="MAX42500 Monitor (no-OS IIO edition) | Analog Devices",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(side="bottom", anchor="w",
                                                             padx=10, pady=(0, 4))

        # Log
        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.pack(fill="both", expand=True, padx=10, pady=(4, 2))
        options = ttk.Frame(log_frame)
        options.pack(fill="x")
        ttk.Checkbutton(options, text="Show Data", variable=self.show_data,
                        command=self._schedule_save).pack(side="left", padx=6)
        ttk.Button(options, text="Clear Log",
                   command=lambda: self.log.delete("1.0", "end")).pack(side="right", padx=6, pady=2)
        text_frame = ttk.Frame(log_frame)
        text_frame.pack(fill="both", expand=True, padx=5, pady=5)
        self.log = tk.Text(text_frame, bg=BG, fg=FG, font=("Consolas", 9), height=8,
                           width=40, insertbackground=FG, selectbackground=ACCENT,
                           relief="flat", highlightthickness=1, highlightbackground=BORDER,
                           highlightcolor=BORDER, borderwidth=0)
        scrollbar = ttk.Scrollbar(text_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for tag, color in (("ok", "#7EE787"), ("fail", "#FF7B72"),
                           ("data", "#8B949E"), ("info", "#79C0FF"), ("sent", "#D2A8FF")):
            self.log.tag_configure(tag, foreground=color)

    # -------------------------------------------------------- ports/URI
    def refresh_ports(self):
        ports = list(serial.tools.list_ports.comports())
        suggestions = [f"serial:{p.device},115200,8n1n" for p in ports]
        self.uri_box["values"] = suggestions + ["ip:analog"]
        if ports:
            entries = [f"{p.device} ({p.description})" for p in ports]
            self._log("Ports: " + ", ".join(entries), "info")
        else:
            self._log("Ports: none found (type an ip: URI, or check the cable)", "fail")

    # -------------------------------------------------------- connection
    def toggle_connection(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.status_label.config(text="Disconnecting", foreground=AMBER)
            return
        uri = self.uri_var.get().strip()
        if not uri:
            self._log("No URI given", "fail")
            return
        self._reset_indicators()
        self.stop_event = threading.Event()
        self.cmd_q = queue.Queue()
        self.worker = IIOWorker(uri, self.cmd_q, self.evt_q, self.stop_event,
                                int(self.poll_var.get()))
        self.worker.start()
        self.connect_button.config(text="Disconnect")
        self.status_label.config(text="Opening", foreground=AMBER)
        self._schedule_save()

    def _send(self, command):
        if not self.connected:
            return False
        self.cmd_q.put(command)
        return True

    # ---------------------------------------------------------- settings
    def _driver_values(self, row):
        """Solve the row's V-Divider + rail OV/UV into the engineering units the
        no-OS driver accepts. Returns (result, nominal, ov, uv). nominal is None
        for IN6/IN7, and all three values are None unless result.status == "ok".
        pct channels: nominal in volts, OV/UV in percent of nominal.
        abs channels: OV/UV in volts (no nominal)."""
        res = compute(row.spec, row.divider.get(), row.ov.get(), row.uv.get())
        if res.status != "ok":
            return res, None, None, None
        if row.spec.kind == "pct":
            nominal = round(res.nom_pin, 6)
            ov = round((res.ov_pin / res.nom_pin - 1.0) * 100.0, 3)
            uv = round((1.0 - res.uv_pin / res.nom_pin) * 100.0, 3)
        else:
            nominal = None
            ov = round(res.ov_pin, 6)
            uv = round(res.uv_pin, 6)
        return res, nominal, ov, uv

    def _paint_row(self, index, res):
        """Colour the row's entries and update the solved-value readout."""
        row = self.rows[index]
        ratio_bad = res.ratio is None and res.status != "empty"
        row.divider_entry.configure(foreground=RED if ratio_bad else FG)
        row.ov_entry.configure(foreground=RED if res.ov_bad else FG)
        row.uv_entry.configure(foreground=RED if res.uv_bad else FG)
        if res.status == "ok":
            if row.spec.kind == "pct":
                text = (f"nom {res.nom_pin:.3f}  OV {res.ov_pin:.3f}  "
                        f"UV {res.uv_pin:.3f}")
            else:
                text = f"OV {res.ov_pin:.3f}  UV {res.uv_pin:.3f}"
            row.calc_label.configure(text=text, foreground=MUTED)
        elif res.status == "empty":
            row.calc_label.configure(text="—", foreground=MUTED)
        else:
            row.calc_label.configure(text="out of range", foreground=RED)

    def _validate_row(self, index):
        """Recompute and recolour one row. Returns the compute() Result."""
        row = self.rows[index]
        res = compute(row.spec, row.divider.get(), row.ov.get(), row.uv.get())
        self._paint_row(index, res)
        return res

    def _channel_mask(self, predicate):
        return sum(1 << i for i, row in enumerate(self.rows) if predicate(row))

    def _send_modes(self, note):
        self._send(("modes", self._channel_mask(ChannelRow.monitored),
                    self._channel_mask(ChannelRow.mapped), note))

    def _send_row(self, index, note):
        row = self.rows[index]
        res, nominal, ov, uv = self._driver_values(row)
        self._paint_row(index, res)
        if res.status == "empty":
            return True  # no limits entered for this channel; nothing to send
        if res.status != "ok":
            self._log(f"{row.spec.name}: not sent, {res.message}", "fail")
            return False
        self._send(("thresholds", index, nominal, ov, uv, note))
        return True

    def _on_poll_changed(self):
        self._schedule_save()
        if self.connected:
            self._send(("poll_hz", int(self.poll_var.get())))

    def _on_commit(self, index):
        row = self.rows[index]
        current = row.field_text()
        if current == row.committed:
            return
        row.committed = current
        self._validate_row(index)
        self._schedule_save()
        if self.auto_set.get() and self.connected:
            if self._send_row(index, f"{row.spec.name} Auto Set"):
                self._set_sync("Sent", GREEN)
        else:
            self._mark_unsent()

    def _on_mode_changed(self):
        self.root.focus_set()
        self._schedule_save()
        self._repaint_channels()
        self._publish_bridge()
        if self.auto_set.get() and self.connected:
            self._send_modes("Auto Set")
            self._set_sync("Sent", GREEN)
        else:
            self._mark_unsent()

    def _on_wd_enable(self):
        self._send(("wd_enable", 1 if self.wd_enable.get() else 0))

    def force_set(self):
        if not self.connected:
            return
        self.root.focus_set()
        self._send(("poll_hz", int(self.poll_var.get())))
        skipped = []
        for index, row in enumerate(self.rows):
            row.committed = row.field_text()
            if not self._send_row(index, f"{row.spec.name} Force Set"):
                skipped.append(row.spec.name)
        self._send_modes("Force Set")
        if skipped:
            self._set_sync(f"Sent, skipped {', '.join(skipped)}", AMBER)
        else:
            self._set_sync("Sent", GREEN)

    def read_thresholds(self):
        if self._send(("read_config",)):
            self._set_sync("Reading chip...", MUTED)

    def _mark_unsent(self):
        if self.connected:
            self._set_sync("Unsent changes", AMBER)

    def _set_sync(self, text, color):
        self.sync_label.config(text=text, foreground=color)

    # ------------------------------------------------------------ config
    def _schedule_save(self):
        if getattr(self, "save_job", None) is not None:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(500, self.save_config)

    def save_config(self):
        self.save_job = None
        config = {
            "version": CONFIG_VERSION,
            "uri": self.uri_var.get().strip(),
            "poll_hz": int(self.poll_var.get()),
            "auto_set": self.auto_set.get(),
            "show_data": self.show_data.get(),
            "bridge_enabled": self.bridge_enabled.get(),
            "bridge_path": self.bridge_path_var.get().strip(),
            "channels": [row.to_config() for row in self.rows],
        }
        try:
            CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
        except OSError as exc:
            self._log(f"Could not save config: {exc}", "fail")

    # ------------------------------------------------------------ bridge
    def _on_bridge_toggle(self):
        if self.bridge_enabled.get():
            path = self.bridge_path_var.get().strip()
            if not path:
                self._log("Bridge: no path set", "fail")
                self.bridge_enabled.set(False)
                return
            self.bridge.set_path(path)
            self.bridge_label.config(text=f"-> {path}", foreground=MUTED)
            self._log(f"Bridge enabled -> {path}", "info")
            self._publish_bridge(force=True)
        else:
            self.bridge.set_path(None)
            self.bridge_label.config(text="(disabled)", foreground=MUTED)
            self._log("Bridge disabled", "info")
        self._schedule_save()

    def _on_bridge_path(self):
        path = self.bridge_path_var.get().strip()
        if self.bridge_enabled.get() and path:
            self.bridge.set_path(path)
            self.bridge_label.config(text=f"-> {path}")
            self._publish_bridge(force=True)
        self._schedule_save()

    def _browse_bridge(self):
        initial = self.bridge_path_var.get().strip() or DEFAULT_BRIDGE_PATH
        chosen = filedialog.asksaveasfilename(
            title="OpenArm bridge JSON location",
            initialfile=Path(initial).name,
            initialdir=str(Path(initial).parent),
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if chosen:
            self.bridge_path_var.set(chosen)
            self._on_bridge_path()

    def _bridge_state(self):
        now = time.monotonic()
        fresh = (self.connected and self.last_data_time is not None
                 and now - self.last_data_time <= DATA_FRESH_S)
        channels = {}
        power_faults, monitor_faults = [], []
        for bit, row in enumerate(self.rows):
            mode = row.mode.get()
            entry = {"mode": mode, "ov": None, "uv": None, "off": None, "fault": None}
            if fresh and self.last_status is not None and mode != MODE_DISABLE:
                ov, uv, off = self.last_status
                mask = 1 << bit
                fault = bool((ov | uv) & mask)
                entry.update(ov=bool(ov & mask), uv=bool(uv & mask),
                             off=bool(off & mask), fault=fault)
                if fault:
                    (power_faults if mode == MODE_ENABLE else monitor_faults).append(
                        row.spec.name)
            channels[row.spec.name] = entry
        power_fault = bool(power_faults)
        if power_fault and not self.bridge_prev_fault:
            self.fault_events += 1
            self.last_fault_time = round(time.time(), 3)
        self.bridge_prev_fault = power_fault
        return {
            "gui_running": True, "connected": self.connected, "data_fresh": fresh,
            "power_fault": power_fault,
            "active_faults": len(power_faults) if fresh else None,
            "fault_channels": power_faults,
            "monitor_fault": bool(monitor_faults),
            "monitor_fault_channels": monitor_faults,
            "fault_events": self.fault_events,
            "last_fault_time": self.last_fault_time,
            "reset_asserted": self.reset_asserted if fresh else None,
            "device_id": f"0x{self.device_id:02X}" if self.device_id is not None else None,
            "channels": channels,
        }

    def _publish_bridge(self, force=False):
        if not self.bridge_enabled.get():
            return
        state = self._bridge_state()
        self.bridge.publish(state, force=force)
        if self.bridge.error:
            if self.bridge_error_logged != self.bridge.error:
                self.bridge_error_logged = self.bridge.error
                self._log(f"Bridge write failed ({self.bridge_path_var.get().strip()}): "
                          f"{self.bridge.error}", "fail")
            self.bridge_label.config(foreground=AMBER)
            return
        if self.bridge_error_logged is not None:
            self._log("Bridge writes recovered", "info")
            self.bridge_error_logged = None
        if state.get("power_fault"):
            self.bridge_label.config(foreground=RED)
        elif not state.get("data_fresh"):
            self.bridge_label.config(foreground=AMBER)
        else:
            self.bridge_label.config(foreground=GREEN)

    def publish_bridge_closed(self):
        if not self.bridge_enabled.get():
            return
        state = self._bridge_state()
        state.update(gui_running=False, connected=False, data_fresh=False,
                     reset_asserted=None)
        for entry in state["channels"].values():
            entry.update(ov=None, uv=None, off=None, fault=None)
        state.update(power_fault=False, active_faults=None, fault_channels=[],
                     monitor_fault=False, monitor_fault_channels=[])
        self.bridge.publish(state, force=True)

    # ------------------------------------------------------------ events
    def _poll_events(self):
        try:
            while True:
                event = self.evt_q.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self._publish_bridge()
        self.root.after(80, self._poll_events)

    def _handle_event(self, event):
        kind = event[0]
        if kind == "stage":
            self._set_stage(event[1], event[2], event[3])
        elif kind == "info":
            self.connected = True
            self.status_label.config(text="Connected", foreground=GREEN)
            self.force_button.config(state="normal")
            self.read_button.config(state="normal")
            # Device-state and watchdog controls stay greyed out on purpose: this
            # GUI mirrors the old Blue Pill firmware, which only *read* CONFIG2
            # (EN0/EN1, shown in the device panel) and WDSTAT (0x26) and never
            # wrote device_state or operated the watchdog. The widgets (state_box,
            # hold_box, wd_check, wd_refresh_btn) and their handlers are kept for
            # reference but remain disabled, so no device_state / watchdog_enable /
            # watchdog_reset_hold / key-refresh write is ever issued.
            self._log(event[1], "info")
            self._set_sync("Not sent yet", AMBER)
        elif kind == "error":
            self._log(event[1], "fail")
        elif kind == "closed":
            self.connected = False
            self.status_label.config(text="Disconnected", foreground=RED)
            self.connect_button.config(text="Connect")
            for widget in (self.force_button, self.read_button, self.state_box,
                           self.hold_box, self.wd_check, self.wd_refresh_btn):
                widget.config(state="disabled")
            self._log(event[1], "info")
            self._set_sync("Not connected", MUTED)
        elif kind == "connected":
            self._apply_snapshot(event[1])
        elif kind == "config":
            self._report_config(event[1])
        elif kind == "write":
            name, ok, note = event[1], event[2], event[3]
            self._log(f"{'OK ' if ok else 'FAIL'} {name}: {note}", "ok" if ok else "fail")
            if not ok:
                self._set_sync("Write failed, see log", RED)
        elif kind == "data":
            self._apply_data(event[1])

    def _apply_snapshot(self, snap):
        self.device_id = snap.device_id
        for spec in CHANNEL_SPECS:
            self.rows[spec.index].ranges = snap.availables.get(spec.index, {})
        for i in range(len(self.rows)):
            self._validate_row(i)
        # Reflect the chip's current watchdog / state if the worker read them
        self._log("Chip ranges read; GUI fields validated against them.", "info")

    def _report_config(self, snap):
        self._log("Chip thresholds read back:", "info")
        mismatches = 0
        for spec in CHANNEL_SPECS:
            row = self.rows[spec.index]
            chip = snap.thresholds.get(spec.index, {})
            res, gui_nominal, gui_ov, gui_uv = self._driver_values(row)
            diffs = []
            if res.status == "ok":
                if spec.has_nominal and gui_nominal is not None \
                        and chip.get("nominal") is not None \
                        and abs(chip["nominal"] - gui_nominal) > 0.02:
                    diffs.append("nominal")
                # OV/UV are percent for pct channels, volts for abs channels.
                tol = 0.05 if spec.kind == "abs" else 0.3
                for key, gui_val in (("ov", gui_ov), ("uv", gui_uv)):
                    chip_val = chip.get(key)
                    if chip_val is not None and gui_val is not None \
                            and abs(chip_val - gui_val) > tol:
                        diffs.append(key)
            # Mode vs VMON/RSTMAP
            mask = 1 << spec.index
            if snap.vmon is not None and snap.rstmap is not None:
                if not (snap.vmon & mask):
                    chip_mode = MODE_DISABLE
                elif snap.rstmap & mask:
                    chip_mode = MODE_ENABLE
                else:
                    chip_mode = MODE_MONITOR
                if chip_mode != row.mode.get():
                    diffs.append(f"mode({chip_mode})")
            nom_s = "  n/a " if not spec.has_nominal or chip.get("nominal") is None \
                else f"{chip['nominal']:.3f}"
            ov_s = "  --  " if chip.get("ov") is None else f"{chip['ov']:.3f}"
            uv_s = "  --  " if chip.get("uv") is None else f"{chip['uv']:.3f}"
            unit = "%" if spec.kind == "pct" else "V"
            if diffs:
                mismatches += 1
                self._log(f"  {spec.name}  nom={nom_s}V ov={ov_s}{unit} uv={uv_s}{unit}"
                          f"   differs: {', '.join(diffs)}", "fail")
            else:
                self._log(f"  {spec.name}  nom={nom_s}V ov={ov_s}{unit} uv={uv_s}{unit}"
                          f"   match", "ok")
        if mismatches:
            self._set_sync("Chip differs from GUI", AMBER)
        else:
            self._set_sync("Chip matches GUI", GREEN)

    def _apply_data(self, regs):
        self.device_id = regs["id"]
        self.last_data_time = time.monotonic()
        self.id_label.config(text=f"ID: 0x{regs['id']:02X}")
        self.raw_label.config(
            text=f"STATOV=0x{regs['statov']:02X}  STATUV=0x{regs['statuv']:02X}  "
                 f"STATOFF=0x{regs['statoff']:02X}\n"
                 f"WDSTAT=0x{regs['wdstat']:02X}  CONFIG2=0x{regs['config2']:02X}  "
                 f"VMON=0x{regs['vmon']:02X}")
        self.last_status = (regs["statov"], regs["statuv"], regs["statoff"])
        self._repaint_channels()
        config2 = regs["config2"]
        rstf = bool(config2 & (1 << CFG2_RSTF))
        self._paint(self.rstf_led, RED if rstf else INACTIVE)
        self.rstf_label.config(text="ACTIVE" if rstf else "clear",
                               foreground=RED if rstf else FG)
        rst_bit = bool(config2 & (1 << CFG2_RST))
        pin_high = rst_bit if RST_BIT_MEANS_PIN_HIGH else not rst_bit
        self.reset_asserted = not pin_high
        self._paint(self.rst_pin_led, INACTIVE if pin_high else RED)
        self.rst_pin_label.config(text="HIGH (released)" if pin_high else "LOW (asserted)",
                                  foreground=FG if pin_high else RED)
        self.en_label.config(text=f"EN0: {int(bool(config2 & (1 << CFG2_EN0)))}   "
                                  f"EN1: {int(bool(config2 & (1 << CFG2_EN1)))}")
        if self.show_data.get():
            self._log("DATA " + " ".join(f"{k}=0x{v:02X}" for k, v in regs.items()), "data")

    # ----------------------------------------------------------- display
    def _set_stage(self, key, ok, detail):
        widgets = self.stage_widgets.get(key)
        if widgets is None:
            return
        led, label = widgets
        self._paint(led, AMBER if ok is None else (GREEN if ok else RED))
        label.config(text=detail or ("OK" if ok else "FAIL"),
                     foreground=FG if ok else (MUTED if ok is None else RED))

    def _reset_indicators(self):
        for led, label in self.stage_widgets.values():
            self._paint(led, NO_DATA)
            label.config(text="waiting", foreground=MUTED)
        self.last_status = None
        self.last_data_time = None
        self.reset_asserted = None
        self._repaint_channels()
        self._paint(self.rstf_led, NO_DATA)
        self._paint(self.rst_pin_led, NO_DATA)
        self.rstf_label.config(text="--", foreground=FG)
        self.rst_pin_label.config(text="--", foreground=FG)
        self.en_label.config(text="EN0: --   EN1: --")
        self.id_label.config(text="ID: --")
        self.raw_label.config(text="STATOV=--  STATUV=--  STATOFF=--\n"
                                   "WDSTAT=--  CONFIG2=--  VMON=--")

    def _repaint_channels(self):
        faults = 0
        for bit, row in enumerate(self.rows):
            mode = row.mode.get()
            row.name_label.config(foreground=FG if mode == MODE_ENABLE else MUTED)
            if row.mode_box is not None:
                row.mode_box.configure(style=f"{mode}.TCombobox")
            if mode == MODE_DISABLE:
                for led in row.leds.values():
                    self._paint(led, INACTIVE)
                continue
            if self.last_status is None:
                for led in row.leds.values():
                    self._paint(led, NO_DATA)
                continue

            def shade(color, faint=(mode == MODE_MONITOR)):
                return FAINT.get(color, color) if faint else color

            ov, uv, off = self.last_status
            mask = 1 << bit
            in_fault = bool((ov | uv) & mask)
            self._paint(row.leds["OV"], shade(RED if ov & mask else INACTIVE))
            self._paint(row.leds["UV"], shade(RED if uv & mask else INACTIVE))
            self._paint(row.leds["OFF"], shade(AMBER if off & mask else INACTIVE))
            self._paint(row.leds["FAULT"], shade(RED if in_fault else GREEN))
            if mode == MODE_ENABLE:
                faults += int(in_fault)
        if self.last_status is None:
            self.fault_counter.config(text="Active Fault: --", fg=MUTED)
        else:
            self.fault_counter.config(text=f"Active Fault: {faults}",
                                      fg=RED if faults else GREEN)

    def _log(self, text, tag=None):
        entry = f"[{time.strftime('%H:%M:%S')}] {text}\n"
        if tag:
            self.log.insert("end", entry, tag)
        else:
            self.log.insert("end", entry)
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > 2000:
            self.log.delete("1.0", f"{line_count - 2000}.0")
        self.log.see("end")


def parse_args(argv):
    """Minimal CLI: [uri] and --bridge <path> (also --bridge=<path>, -b).

    Returns (uri_or_None, bridge_path_or_None). A bridge path enables the bridge.
    """
    uri, bridge, positionals = None, None, []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--bridge", "-b") and i + 1 < len(argv):
            bridge = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--bridge="):
            bridge = arg.split("=", 1)[1]
            i += 1
            continue
        positionals.append(arg)
        i += 1
    if positionals:
        uri = positionals[0]
    return uri, bridge


def main():
    cli_uri, cli_bridge = parse_args(sys.argv[1:])
    root = tk.Tk()
    app = App(root, cli_uri=cli_uri, cli_bridge=cli_bridge)

    def on_close():
        if getattr(app, "save_job", None) is not None:
            root.after_cancel(app.save_job)
        app.save_config()
        app.publish_bridge_closed()
        app.stop_event.set()
        root.after(300, root.destroy)

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
