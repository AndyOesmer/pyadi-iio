# CN0556
## 1. Install Prerequisite:

```cmd
pip install -r requirements.txt
pip install -e .
```
From the pyadi-iio main folder, install the prerequisites.

```cmd
cd examples/cn0556
pip install -r requirements.txt
```
Change the directory to where the example scripts are saved. After running this command, pip will read the requirements.txt file and install the specified packages along with their dependencies.

## 2. Run Example Script:
### Run CN0556 board in Buck Mode
This will perform an example of setting the CN0556 board in Buck Mode.
```
python cn0556_example_buck.py
```
By default, this script will produce a regulated voltage output of 14V at the V2 right side, when a voltage input of 54V-56V is connected to the V1 Buck Input side.

### Run CN0556 board in Boost Mode
This will perform an example of setting the CN0556 board in Boost Mode.
```
python cn0556_example_boost.py
```
By default, this script will produce a regulated voltage output of 56V at the V1 left side, when a voltage input of 12V-14V is connected to the V2 Boost Input side.
