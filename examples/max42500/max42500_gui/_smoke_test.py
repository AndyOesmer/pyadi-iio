"""Headless smoke test for the IIO worker (no Tkinter window).

Connects the IIOWorker to a URI, lets it run briefly, and prints the events it
emits (stages, snapshot, a few polled DATA frames). Exercises the real
adi.max42500 + reg_read path without opening the GUI.

    python _smoke_test.py serial:COM4,115200,8n1n
"""
import queue
import sys
import threading
import time

import max42500_monitor_gui as g

uri = sys.argv[1] if len(sys.argv) > 1 else "serial:COM4,115200,8n1n"
cmd_q, evt_q, stop = queue.Queue(), queue.Queue(), threading.Event()

print(f"Connecting worker to {uri} ...", flush=True)
worker = g.IIOWorker(uri, cmd_q, evt_q, stop, poll_hz=5)
worker.start()

data_frames = 0
deadline = time.time() + 8
try:
    while time.time() < deadline:
        try:
            ev = evt_q.get(timeout=0.2)
        except queue.Empty:
            continue
        kind = ev[0]
        if kind == "stage":
            print(f"  STAGE {ev[1]:5} ok={ev[2]!s:5} {ev[3]}", flush=True)
        elif kind == "connected":
            snap = ev[1]
            print(f"  SNAPSHOT id=0x{(snap.device_id or 0):02X} "
                  f"vmon={snap.vmon} rstmap={snap.rstmap}", flush=True)
            for idx, vals in sorted(snap.thresholds.items()):
                rng = snap.availables.get(idx, {})
                print(f"    IN{idx+1}: values={vals} ranges={rng}", flush=True)
        elif kind == "data":
            data_frames += 1
            if data_frames <= 3:
                print("  DATA " + " ".join(f"{k}=0x{v:02X}"
                      for k, v in ev[1].items()), flush=True)
        elif kind in ("info", "error", "closed"):
            print(f"  {kind.upper()}: {ev[1]}", flush=True)
            if kind in ("error", "closed"):
                break
finally:
    stop.set()
    worker.join(timeout=2)

print(f"Done. Received {data_frames} DATA frame(s).", flush=True)
sys.exit(0 if data_frames > 0 else 1)
