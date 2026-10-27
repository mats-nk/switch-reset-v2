# Switch Reset v2

A Python tool for authenticated factory reset over a serial console or SSH, with model-specific, documented recovery guides. Cisco Catalyst and HP ProCurve/AOS-Switch are the primary CLI profiles. Other profiles are best-effort command templates and must be checked against the exact model and software release.

## Safety and scope

- Factory reset is destructive: it can erase VLANs, trunks, management addresses, credentials, and other configuration, and interrupt service.
- The tool does not brute-force credentials or silently bypass authentication.
- Bootloader/ROMMON procedures are provided as model-scoped guides. The program does **not** automatically interrupt boot, type bootloader commands, erase flash, or confirm a destructive bootloader prompt. This is intentional: model families have materially different procedures.
- A password-recovery procedure is not necessarily a factory reset. For example, Cisco Catalyst 9000 `SWITCH_IGNORE_STARTUP_CFG=1` is documented for bypassing startup configuration during password recovery and does not by itself erase the saved configuration.
- Exact hardware model, software release, stack/chassis topology, and vendor documentation must be checked before using a recovery guide.
- SSH host keys must already be trusted in the system `known_hosts` database; unknown keys are rejected.
- Test command profiles on non-production equipment first. The script cannot guarantee every CLI prompt or command is identical across software releases.

## Requirements

- Python 3.10 or newer
- `pyserial` for serial console connections
- `paramiko` for SSH connections

The project does not require Fedora or any other particular Linux distribution. Python and the dependencies are also available on Windows and macOS. Serial device names and permissions vary by operating system.

## Installation

### Portable installation with Python's venv and pip

Linux, macOS, and Windows (PowerShell):

```text
python -m venv .venv
```

Activate the environment:

Linux/macOS:

```bash
. .venv/bin/activate
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```text
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Alternatively, use `uv` if already installed:

```text
uv venv
uv pip install -r requirements.txt
```

On Linux, ensure the current user has permission to open the chosen serial device; the required group and permission mechanism depend on the distribution. On Windows, use the COM port shown by Device Manager. On macOS, serial adapters commonly appear under `/dev/cu.*`.

The serial-port picker excludes Linux virtual consoles `/dev/tty0` through `/dev/tty59` and considers supported serial devices such as `/dev/ttyUSB*`, `/dev/ttyACM*`, `/dev/ttyS*`, and Windows `COM` ports. The operating system's own permissions still apply.

## Run

Interactive connection and platform selection:

```text
python switch_reset.py
```

SSH example (the script prompts for credentials):

```text
python switch_reset.py --transport ssh --platform cisco --host 192.0.2.10
```

Serial mode:

```text
python switch_reset.py --transport serial --platform hp
```

## Documented recovery guides (no device changes)

Cisco Catalyst 9000 ROMMON and documented default-reset decision point:

```text
python switch_reset.py --bootloader-guide cisco-cat9k
```

Legacy Cisco Catalyst bootloader recovery:

```text
python switch_reset.py --bootloader-guide cisco-legacy
```

HP ProCurve 1700/1800 documented hardware factory reset (not ROMMON):

```text
python switch_reset.py --bootloader-guide hp-procurve-1700-1800
```

General recovery references:

```text
python switch_reset.py --recovery-guide
python switch_reset.py --recovery-guide --platform cisco
python switch_reset.py --recovery-guide --platform hp
```

These guide commands do not connect to or modify a device.

## Profiles

- `cisco`: Catalyst IOS/IOS-XE CLI reset template.
- `hp`: ProCurve / ArubaOS-Switch CLI reset template.
- `arubacx`, `juniper`, `huawei`, `openwrt`: model-dependent command templates, not universal guarantees.

## Network transport notes

SSH provides an authenticated interactive CLI shell; it is not a vendor API reset. The tool intentionally does not use Telnet. OpenWrt SSH shell behavior differs from a managed switch CLI, and reset behavior varies by image and device.

## Official recovery references

- Cisco Catalyst 9000 ROMMON/password recovery and reset prompt: https://www.cisco.com/c/en/us/support/docs/switches/catalyst-9300-series-switches/216944-troubleshoot-bootloader-rommon-and-pas.html
- Cisco legacy Catalyst password recovery: https://www.cisco.com/c/en/us/support/docs/switches/catalyst-2950-series-switches/12040-pswdrec-2900xl.html
- HPE ProCurve 2520 factory default: https://support.hpe.com/hpesc/public/docDisplay?docId=c01983148&docLocale=en_US
- HPE ProCurve 1700/1800 hardware reset: https://support.hpe.com/hpesc/public/docDisplay?docId=c01752898&docLocale=en_US
- ArubaOS-Switch factory-default procedure: https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/MCG/WC/content/common%20files/res-fac-def-con.htm
- ArubaOS-Switch front-panel security controls: https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/MCG/KB/content/kb/fro-pan-sec-pas-cle.htm


### Serial-port selection

On Linux, the script excludes `/dev/tty0` through `/dev/tty59` (virtual consoles) and `/dev/ttyS0` through `/dev/ttyS59` (conventional UART devices). The serial menu targets USB console adapters such as `/dev/ttyUSB0` and `/dev/ttyACM0`. Windows `COM` ports are also supported.

## Post-reset boot monitor and reset history

After the reset commands are sent, the script monitors the reboot:

- **Serial console:** displays live boot output and stops when it recognizes a likely login/CLI-ready marker, a possible boot failure, or the timeout. This is a heuristic; inspect the output and verify the device manually.
- **SSH:** watches the SSH TCP port for an outage followed by recovery. This confirms only that the port became reachable again, not that login works or that the device reached a healthy CLI. A serial console is preferred when boot diagnostics are required.
- The default monitor timeout is 300 seconds. Override it with `--boot-monitor-timeout 600` for slower devices.

Before the destructive confirmation, the script asks for the hardware model/product ID, software/firmware version, and optional notes. These values are initial entries. When using the serial console, the post-reset boot monitor parses captured output for supported model, software version, software image, and system serial number fields. Detected values replace the initial values in the database; if parsing does not find a value, the manually entered value is preserved. Detection is pattern-based and should be checked against the device output.

For Cisco IOS/IOS-XE, parsing recognizes fields such as `Model number`, `System serial number`, the `SW Version` column, and `SW Image` column. For SSH, the current monitor only checks TCP reachability after reboot, so it cannot automatically read device facts; values remain as entered.

Reset records are stored in a local SQLite database, `switch_reset_history.sqlite3` in the current working directory by default. The database records local date/time, platform, hardware model, software version, software image, system serial number, connection method/endpoint, reset-command outcome, boot-monitor outcome, and notes. Existing databases are upgraded with the new inventory columns automatically, without deleting existing records. It does **not** store passwords. Keep the database backed up and protect it appropriately because it may contain device addresses and inventory information.

List the latest 50 records:

```bash
python switch_reset.py --list-history
```

Use a different database file:

```bash
python switch_reset.py --database ./inventory/reset-history.sqlite3
python switch_reset.py --list-history --database ./inventory/reset-history.sqlite3
```

The database records that reset commands were sent; it does not certify that every device reached factory defaults. A `console-ready-marker` is a pattern match, not a full vendor health check. For stronger verification, capture the exact model/software-specific boot and post-reset checks.
