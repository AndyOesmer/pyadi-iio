# MAX42500 examples — setup from a clean machine

How to go from a **laptop with nothing installed** to running the MAX42500
examples (the basic script and the GUI) against a Maxim MCU running the MAX42500
**no-OS IIO** firmware.

The setup has two sides:

```
  [ MCU board ]  AD-APARD32690-SL / MAX32666FTHR          [ Host laptop ]
  runs no-OS "iio_example" firmware  ──USB serial──►  pyadi-iio + libiio
  (TinyIIOD server over UART)        serial:COMx,115200,8n1n   examples / GUI
```

> **Important:** both host programs — the *basic* `max42500_example.py` **and**
> the GUI — are IIO clients. The MCU must run the no-OS **`iio_example`** variant
> (the TinyIIOD server). The no-OS **`basic`** variant is a standalone console
> demo and does **not** speak IIO, so the host scripts can't talk to it.

Commands below are **Windows PowerShell** (the tested environment); Linux/macOS
notes are called out where they differ.

---

## Part A — Install the required apps on the laptop (once)

| Tool | Needed for | Notes |
|---|---|---|
| **Python 3.8+** (3.11 tested) | everything host-side | Install from python.org. **Tick "Add to PATH"**, and keep the **tcl/tk** option (gives `tkinter`, required by the GUI). |
| **libiio runtime (0.25)** | pyadi-iio to talk to the board | The C library. On Windows, install the ADI **libiio 0.25** package or **IIO Oscilloscope** (bundles `libiio.dll` + `iio_info.exe`). |
| **git** | getting the pyadi-iio source | Or download the repo zip. |
| **uv** *(optional)* | faster installs | `pip install uv` or see astral.sh/uv. Speeds up Parts C. |
| **Analog Devices MSDK** *(only if you flash the firmware yourself)* | OpenOCD + ARM toolchain + target configs | Not needed if the board already has the IIO firmware. |

Verify the two essentials:

```powershell
python --version
& "C:\Program Files\IIO Oscilloscope\bin\iio_info.exe" --version   # expect "Libiio version: 0.25"
```

> On **Linux/macOS**: install libiio via your package manager (`apt install
> libiio-utils`, `brew install libiio`, …), check with `iio_info --version`, and
> make sure the pin in `requirements.txt` matches that version. On Linux also
> `sudo apt install python3-tk` for the GUI.

---

## Part B — Flash the no-OS IIO firmware to the MCU (step 1)

You need a board with a connected **SWD debug probe** (the AD-APARD32690-SL has
an onboard CMSIS-DAP debugger; the MAX32666FTHR needs an external one such as a
MAX32625PICO).

### Option 1 — flash a provided prebuilt `.elf` (fastest)

With the MSDK installed and the probe connected:

```powershell
$env:MAXIM_PATH = "C:\MaximSDK"
& "$env:MAXIM_PATH\Tools\OpenOCD\openocd.exe" `
    -s "$env:MAXIM_PATH\Tools\OpenOCD\scripts" `
    -f interface/cmsis-dap.cfg `
    -f target/max32690.cfg `
    -c "program max42500_iio.elf verify reset exit"
```

- Use the target cfg for your part: **`target/max32690.cfg`** for the
  AD-APARD32690-SL; the **MAX3266x** family cfg for the MAX32666FTHR.
- `max42500_iio.elf` is the prebuilt IIO firmware you were given.

### Option 2 — build and flash from no-OS source

From the `no-OS` repo (needs the MSDK; see its *Maxim CMake build guide*):

```powershell
$env:MAXIM_LIBRARIES = "C:\MaximSDK\Libraries"
cd C:\Users\aoesmer\git\max42500\no-OS

# build + flash the IIO firmware on the APARD32690
python tools/scripts/no_os_build.py build `
   --project max42500 --variant iio_example --board ad-apard32690-sl `
   --probe openocd --flash
```

Boards/variants available: `--board ad-apard32690-sl` (basic, iio_example) or
`max32666fthr` (iio_example). Always use `--variant iio_example` for these host
examples.

### After flashing

The board enumerates as a **USB serial (COM) port**. Note its number — that is
the `COMx` you pass to the host scripts. The TinyIIOD UART runs at **115200**
baud, so the context URI is `serial:COMx,115200,8n1n`.

---

## Part C — Set up pyadi-iio on the host (steps 2, 4, 5)

### 1. Get the source

```powershell
git clone https://github.com/analogdevicesinc/pyadi-iio.git
cd pyadi-iio
# (or use your existing checkout: C:\Users\aoesmer\git\max42500\pyadi-iio)
```

### 2. Create and activate a virtual environment named `env`

```powershell
python -m venv env
& .\env\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

Linux/macOS: `python3 -m venv env && source env/bin/activate`.

### 3. Install the Python dependencies — **pin pylibiio first**

This is the step that avoids the errors we hit (see Troubleshooting). Installing
`pylibiio==0.25` **before** building pyadi-iio stops pip from pulling the broken
default `pylibiio 1.0`.

```powershell
pip install -r examples\max42500\requirements.txt
```

### 4. Build pyadi-iio from source (editable)

```powershell
pip install -e .
```

Use **editable** (`-e`) so edits to `adi/` and the examples take effect without
reinstalling. (`pip install .` also works but is a static copy.)

### Same thing with **uv** (faster)

```powershell
pip install uv                       # if not already installed
uv venv env
& .\env\Scripts\Activate.ps1
uv pip install -r examples\max42500\requirements.txt
uv pip install -e .
```

### 5. Verify the install

```powershell
python -c "import iio; print('libiio bindings', iio.version)"
python -c "import adi, serial; print('pyadi + pyserial OK')"
```

---

## Part D — Run the examples (step 6)

First, find the board's COM port:

```powershell
python -c "import serial.tools.list_ports as p; [print(x.device, x.description) for x in p.comports()]"
```

Then (venv activated), use `serial:COMx,115200,8n1n` — replace `COM4` below.

### Basic example (console)

```powershell
python examples\max42500_example.py serial:COM4,115200,8n1n
```

Configures VM1 and polls its OV/UV/OFF status once per second (Ctrl+C to stop).
If you pipe/redirect its output, run it unbuffered so lines appear live:

```powershell
python -u examples\max42500_example.py serial:COM4,115200,8n1n > log.txt
```

### GUI example

```powershell
python examples\max42500\max42500_gui\max42500_monitor_gui.py
```

Then type the URI in the **URI** box and press **Connect**. Or pass it (and,
optionally, enable the OpenArm bridge) on the command line:

```powershell
# prefill the URI
python examples\max42500\max42500_gui\max42500_monitor_gui.py serial:COM4,115200,8n1n

# also enable + point the OpenArm bridge at a file
python examples\max42500\max42500_gui\max42500_monitor_gui.py serial:COM4,115200,8n1n `
    --bridge C:\OpenArmBridge\system_state.json
```

The GUI renders fully **without** hardware (only Connect needs a board), so you
can inspect the layout anytime.

### Headless smoke test (no window)

Validates the IIO data path without opening the GUI — handy over SSH/CI:

```powershell
python examples\max42500\max42500_gui\_smoke_test.py serial:COM4,115200,8n1n
```

---

## Commands cheat-sheet

```powershell
# one-time host setup
python -m venv env
& .\env\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r examples\max42500\requirements.txt      # pylibiio 0.25 + numpy + pyserial
pip install -e .                                        # build pyadi-iio from source

# every new terminal
& .\env\Scripts\Activate.ps1

# run
python examples\max42500_example.py serial:COM4,115200,8n1n                 # basic
python examples\max42500\max42500_gui\max42500_monitor_gui.py              # GUI
python examples\max42500\max42500_gui\_smoke_test.py serial:COM4,115200,8n1n   # smoke test
```

---

## Troubleshooting (errors we actually hit)

| Symptom | Cause | Fix |
|---|---|---|
| `TypeError: argument of type 'NoneType' is not iterable` at `find_library("c")` on `import iio`, **or** `OSError: libiio version 1.x required` | pip installed **pylibiio 1.0** (libiio 1.x bindings); broken on Windows and wrong API for this pyadi | `pip uninstall -y pylibiio && pip install "pylibiio==0.25"` (match your libiio). Installing `requirements.txt` before `pip install -e .` prevents this. |
| `ModuleNotFoundError: No module named 'serial'` | `pyserial` missing (GUI) | `pip install pyserial` (it's in `requirements.txt`). |
| `Exception: No device found` on connect | wrong COM port, board not flashed/powered, wrong baud, or no hardware | Re-check the COM port (Part D), confirm the **iio_example** firmware is running, use `...,115200,8n1n`. |
| Basic example connects but nothing prints when redirected | stdout is block-buffered when not a terminal | add `-u`: `python -u examples\max42500_example.py ...` |
| GUI opens but Connect fails with no board attached | expected | the GUI still renders fully; attach a board to connect. |
| `pylibiio` import works but attributes/channels missing | libiio C library vs bindings version mismatch | make `iio_info --version` and the `pylibiio==` pin agree. |
