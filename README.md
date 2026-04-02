# CODERDBC 
  
Coderdbc is a CLI utility for generating C code from DBC CAN matrix files

### Features
- ***Pack*** and ***Unpack*** functions for conversion signals to CAN payload raw data and vice verse
- ***Node based*** Receive function _(each node (ECU) has its own ***Receive*** function according to its DBC configuration)_
- Automation on monitoring functions: CRC, counter and missing tests
- Optional source code generation _(the generation of readonly and configuration files can be avoided)_
- Flexible setup via driver configuration _(see comments in source code for details)_

## Build and run

For building project you need to have cmake and c++ development toolkit in your system
1 download source code:
```sh
git clone https://github.com/astand/c-coderdbc.git coderdbc
```
Go to the source code directory:
```sh
cd coderdbc
```
Run cmake configuration to 'build' directory:
```sh
cmake -S src -B build
```
Run cmake build:
```sh
cmake --build build --config release
```
Go to the build directory and run:
```sh
cd build
./coderdbc --help
```

Help information with main instructions about using the tool will be printed

## Driver functionality description

    The source code package includes the following source files (presuming that the dbc driver name is "ecudb"):
      
      ecudb.c / ecudb.h                            (1) RO / lib

    Pair of the main driver which contains all dbc frames structs / pack functions / unpack functions declarations. These source files preferably to place in the share/library directory. This part of the package is non-changable and has no any data, so can be used across multi projects.
    
      ecudb-fmon.h                                 (2) RO / lib

    Fmon header is a readonly part of monitoring part of the package. It contains the list of functions for CAN message validation. Those functions should be defined in the scope of user code and can be optionally used in unpack messages. This file is preferably to place in the share/library directory next to the main driver source files.

      ecudb-fmon.c                                 (3) app

    User specific part of monitoring functionality. If monitoring is fully enabled user code must define all the monitoring functions. This file is a part of the scope of user code.

      ecudb-config.h                               (4) app / inc*

    An application specific configuration file for enabling features in the main driver. If there are a few projects (applications) which include a single main driver (1,2) then each project has to have its own copy of this configuration. Source code (1,2) includes this configuration. If a few dbc matrix is in use in your application then for each of (1,2) specific configuration file must be presented.

      dbccodeconf.h                                (5) app / inc

    Application specific configuration file. This file might include "CanFrame" definition, sigfloat_t typedef and binutil macros which enables rx and tx structures allocation inside ecudb-binutil.c. Each project has to have its own copy of this configuration (see template dbccodeconf.h). Source code (4,6) includes this configuration.

      ecudb-binutil.c / ecudb-binutil.h            (6) RO / app

    The part which is used for generalization CAN frame flow receiving and unpacking. It also optionally can allocate CAN frame tx/rx structs. 
    
      canmonitorutil.h                             (7) lib

    General definitions for monitoring feature. The source file can be place to the share/library directory.
    
    -----------------------------------------------------------------------------------------------

    *inc - file location have to be added to project include path.

## generation options

  There are several available generation option, use '-help' option for details

## GUI (`gui.py`)

A Python desktop GUI is provided in `gui.py` as a graphical front-end to the `coderdbc` CLI binary. It requires [ttkbootstrap](https://ttkbootstrap.readthedocs.io/):

```sh
pip install ttkbootstrap>=1.10.0
python3 gui.py
```

Build the C++ binary first if you haven't already (see **Build and run** above). The GUI will auto-detect it under `build/coderdbc`; if not found you can point to it manually via the *Settings* tab.

### How it works

The GUI is a **Python/tkinter** application (`gui.py`). It does **not** extend or modify the C++ code — the C++ generator API is completely unchanged. All interactive features are implemented in Python and the generator is invoked via the existing CLI interface:

**DBC parsing for preview (Python, no C++ involvement)**

The GUI includes a lightweight Python DBC parser (`DbcParser` class in `gui.py`). When you open a `.dbc` file this parser reads it directly in Python and populates the tree view with all messages and their signals. The `coderdbc` binary is not called at this stage.

**Message preview**

Parsed messages are shown in a resizable tree. Each row shows the message ID, DLC, number of signals, and transmitter. Expanding a message row reveals its individual signals (start bit, length, byte order, value type).

**Group selection**

Messages can be selected or deselected individually. An *Auto-Group* button detects sets of similarly-named messages (e.g. 40 radar-object frames sharing a common name prefix + numeric suffix) and collapses them into a single collapsible group with a tri-state checkbox (all / partial / none selected). A live search bar filters the tree in real time. *Select All* and *Deselect All* buttons are also provided.

**Filtered code generation**

When you click *Generate C Code* the GUI:

1. Determines which message IDs are selected.
2. If all messages are selected, the original `.dbc` file is passed straight to the binary.
3. If only a subset is selected, `DbcParser.write_filtered()` copies the original file to a temporary `.dbc` that contains only the chosen `BO_` blocks (all other DBC sections such as `NS_`, `BU_`, `CM_`, `BA_`, `VAL_` are preserved unchanged). This filtered file is what gets passed to the generator, so only pack/unpack functions for selected messages are emitted.
4. The GUI assembles a `coderdbc` command from the paths and option toggles in the *Settings* tab (all standard CLI flags are exposed: `-rw`, `-nodeutils`, `-driverdir`, `-gendate`, `-noconfig`, `-noinc`, `-nofmon`) and runs it as a subprocess in a background thread.
5. Progress is shown via an indeterminate progress bar and a colour-coded output log pane.

### Why no C++ API changes were needed

All features that appear "interactive" — browsing, previewing, filtering, group detection — are handled entirely in Python before the generator is called. The generator receives only a standard `.dbc` file and standard CLI flags, which is exactly what it already supports. No additions to the C++ API are required.
