# Copyright (C) 2026 Analog Devices, Inc.
#
# SPDX short identifier: ADIBSD

from adi.attribute import attribute
from adi.context_manager import context_manager


class max42500_channel(attribute):
    """MAX42500 voltage-monitor input (VMn).

    VM1-VM5 expose a settable nominal voltage and over/under-voltage
    thresholds in percent. VM6-VM7 have no nominal voltage and their
    thresholds are expressed in volts.
    """

    def __init__(self, ctrl, channel_name, has_nominal_voltage):
        self.name = channel_name
        self._ctrl = ctrl
        self._has_nominal_voltage = has_nominal_voltage

    @property
    def off_status(self):
        """1 if the input is below its OFF (power-down) threshold."""
        return self._get_iio_attr(self.name, "off_status", False)

    @property
    def uv_status(self):
        """1 if an undervoltage condition is detected on the input."""
        return self._get_iio_attr(self.name, "uv_status", False)

    @property
    def ov_status(self):
        """1 if an overvoltage condition is detected on the input."""
        return self._get_iio_attr(self.name, "ov_status", False)

    @property
    def power_up_timestamp(self):
        """Recorded power-up sequence position for this input."""
        return self._get_iio_attr(self.name, "power_up_timestamp", False)

    @property
    def power_down_timestamp(self):
        """Recorded power-down sequence position for this input."""
        return self._get_iio_attr(self.name, "power_down_timestamp", False)

    @property
    def nominal_voltage(self):
        """Nominal (expected) input voltage, in volts (VM1-VM5 only)."""
        if not self._has_nominal_voltage:
            raise AttributeError(
                "nominal_voltage is not available on " + self.name
            )
        return self._get_iio_attr(self.name, "nominal_voltage", False)

    @nominal_voltage.setter
    def nominal_voltage(self, value):
        if not self._has_nominal_voltage:
            raise AttributeError(
                "nominal_voltage is not available on " + self.name
            )
        self._set_iio_attr_float(self.name, "nominal_voltage", False, value)

    @property
    def nominal_voltage_available(self):
        """[min step max] range accepted by nominal_voltage (VM1-VM5 only)."""
        if not self._has_nominal_voltage:
            raise AttributeError(
                "nominal_voltage_available is not available on " + self.name
            )
        return self._get_iio_attr_str(self.name, "nominal_voltage_available", False)

    @property
    def ov_threshold(self):
        """Overvoltage threshold (percent for VM1-VM5, volts for VM6-VM7)."""
        return self._get_iio_attr(self.name, "ov_threshold", False)

    @ov_threshold.setter
    def ov_threshold(self, value):
        self._set_iio_attr_float(self.name, "ov_threshold", False, value)

    @property
    def ov_threshold_available(self):
        """[min step max] range accepted by ov_threshold."""
        return self._get_iio_attr_str(self.name, "ov_threshold_available", False)

    @property
    def uv_threshold(self):
        """Undervoltage threshold (percent for VM1-VM5, volts for VM6-VM7)."""
        return self._get_iio_attr(self.name, "uv_threshold", False)

    @uv_threshold.setter
    def uv_threshold(self, value):
        self._set_iio_attr_float(self.name, "uv_threshold", False, value)

    @property
    def uv_threshold_available(self):
        """[min step max] range accepted by uv_threshold."""
        return self._get_iio_attr_str(self.name, "uv_threshold_available", False)


class max42500(context_manager, attribute):
    """MAX42500 power-system supervisor / voltage monitor.

    The device exposes seven voltage-monitor inputs (VM1-VM7) plus a set of
    device-wide attributes (state machine, watchdog). Access the inputs either
    through the ``channel`` list (``dev.channel[0]`` is VM1) or the ``vm1`` ..
    ``vm7`` convenience attributes.
    """

    _device_name = "max42500"
    _num_channels = 7

    def __init__(self, uri=""):
        """max42500 class constructor.

        Args:
            uri: Context URI, e.g. ``serial:COM4,115200,8n1n`` or ``ip:analog``.
        """
        context_manager.__init__(self, uri, self._device_name)

        self._ctrl = self._ctx.find_device(self._device_name)
        if not self._ctrl:
            raise Exception("No max42500 device found in context " + str(uri))

        self.channel = []
        for i in range(self._num_channels):
            # IIO channels are indexed IIO_VOLTAGE -> ids "voltage0".."voltage6".
            # VM1-VM5 (index 0-4) support a nominal voltage; VM6-VM7 do not.
            chan = max42500_channel(
                self._ctrl, "voltage{}".format(i), has_nominal_voltage=(i < 5)
            )
            self.channel.append(chan)
            setattr(self, "vm{}".format(i + 1), chan)

    # ---- Device-wide (global) attributes --------------------------------

    @property
    def device_state(self):
        """Device state machine: one of ``off``, ``sleep``, ``on``."""
        return self._get_iio_dev_attr_str("device_state")

    @device_state.setter
    def device_state(self, value):
        self._set_iio_dev_attr_str("device_state", value)

    @property
    def device_state_available(self):
        """Valid ``device_state`` strings."""
        return self._get_iio_dev_attr_str("device_state_available")

    @property
    def watchdog_enable(self):
        """Watchdog enable (0/1)."""
        return self._get_iio_dev_attr("watchdog_enable")

    @watchdog_enable.setter
    def watchdog_enable(self, value):
        self._set_iio_dev_attr("watchdog_enable", int(bool(value)))

    @property
    def watchdog_key(self):
        """Current watchdog key value."""
        return self._get_iio_dev_attr("watchdog_key")

    def refresh_watchdog_key(self):
        """Issue a new watchdog key (any write triggers the refresh)."""
        self._set_iio_dev_attr("watchdog_key", 1)

    @property
    def watchdog_reset_hold(self):
        """Watchdog reset-hold time: ``0ms``, ``8ms``, ``16ms`` or ``32ms``."""
        return self._get_iio_dev_attr_str("watchdog_reset_hold")

    @watchdog_reset_hold.setter
    def watchdog_reset_hold(self, value):
        self._set_iio_dev_attr_str("watchdog_reset_hold", value)

    @property
    def watchdog_reset_hold_available(self):
        """Valid ``watchdog_reset_hold`` strings."""
        return self._get_iio_dev_attr_str("watchdog_reset_hold_available")

    # ---- Register debug access ------------------------------------------

    def reg_read(self, addr):
        """Read a device register through the IIO debug interface."""
        return self._ctrl.reg_read(addr)

    def reg_write(self, addr, value):
        """Write a device register through the IIO debug interface."""
        self._ctrl.reg_write(addr, value)

    def __repr__(self):
        return (
            'max42500(uri="{}") object "{}"\n'
            "7-input I2C power-system supervisor / voltage monitor\n\n"
            "channel[0..6] / vm1..vm7:\n"
            "  off_status, uv_status, ov_status      (read only)\n"
            "  power_up_timestamp, power_down_timestamp (read only)\n"
            "  nominal_voltage (+_available)         (r/w, VM1-VM5 only)\n"
            "  ov_threshold, uv_threshold (+_available) (r/w)\n\n"
            "device-wide:\n"
            "  device_state (+_available)            (r/w)\n"
            "  watchdog_enable, watchdog_key         (r/w)\n"
            "  watchdog_reset_hold (+_available)     (r/w)\n".format(
                self.uri, self._device_name
            )
        )
