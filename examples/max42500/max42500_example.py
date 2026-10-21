# Copyright (C) 2026 Analog Devices, Inc.
#
# SPDX short identifier: ADIBSD
"""MAX42500 voltage-monitor example.

Python equivalent of the no-OS ``max42500`` basic example: it configures VM1
(nominal voltage and over/under-voltage thresholds) and then continuously polls
and prints the input's comparator status.

Usage:
    python max42500_example.py [uri]

The URI points at the IIO context exported by the no-OS firmware, e.g. a
serial-backed tinyiiod running on the AD-APARD32690-SL::

    python max42500_example.py serial:COM4,115200,8n1n

or a networked context::

    python max42500_example.py ip:192.168.2.1
"""

import sys
import time

import adi

# Context URI: first CLI argument, else auto-discover.
my_uri = sys.argv[1] if len(sys.argv) >= 2 else ""

print("********** MAX42500 Voltage Monitor Tests **********")

dev = adi.max42500(uri=my_uri)

# VM1 is the first voltage-monitor input (dev.channel[0] / dev.vm1).
vm1 = dev.vm1

# Configure VM1: 2 V nominal, +/-10 % over/under-voltage thresholds.
# (ov/uv thresholds for VM1-VM5 are expressed in percent of nominal.)
vm1.nominal_voltage = 2
vm1.ov_threshold = 10
vm1.uv_threshold = 10

print("VM1 configured:")
print("  nominal_voltage = {} V".format(vm1.nominal_voltage))
print("  ov_threshold    = {} %".format(vm1.ov_threshold))
print("  uv_threshold    = {} %".format(vm1.uv_threshold))
print("Polling VM1 status (Ctrl+C to stop)...")

try:
    while True:
        off_stat = vm1.off_status
        ov_stat = vm1.ov_status
        uv_stat = vm1.uv_status

        if off_stat:
            print("   IN1 status: Voltage is below OFF threshold", flush=True)
        elif uv_stat:
            print("   IN1 status: Undervoltage detected", flush=True)
        elif ov_stat:
            print("   IN1 status: Overvoltage detected", flush=True)
        else:
            print("   IN1 status: Voltage is within acceptable range", flush=True)

        time.sleep(1)
except KeyboardInterrupt:
    print("\nStopped.")
finally:
    del dev
