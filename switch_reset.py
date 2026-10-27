#!/usr/bin/env python3
"""Switch Reset v2: authenticated CLI reset over serial or SSH, with model-specific recovery guides."""
from __future__ import annotations
import argparse, getpass, os, re, sys, time, sqlite3, socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None
try:
    import paramiko
except ImportError:
    paramiko = None

PROMPT = re.compile(rb"(?m)(?:[\w.@:/()\- ]+[>#%]|<[^>]+>|\[[^\]]+\])\s*$")
LOGIN_FAIL = re.compile(rb"(?i)(authentication failed|login incorrect|invalid password|access denied|bad passwords|invalid username or password)")

@dataclass
class Profile:
    key: str
    name: str
    baud: int
    commands: list[tuple[str, list[tuple[str, str]]]]
    notes: str

PROFILES = {
    "cisco": Profile("cisco", "Cisco Catalyst IOS/IOS-XE", 9600, [
        ("write erase", [(r"\[confirm\]", "")]),
        ("delete flash:vlan.dat", [(r"Delete filename.*\?", ""), (r"\[confirm\]", "")]),
        ("reload", [(r"\[yes/no\]", "no"), (r"Proceed with reload\?\s*\[confirm\]", "")]),
    ], "Erases startup configuration and VLAN database where applicable; not a firmware wipe."),
    "hp": Profile("hp", "HP ProCurve / ArubaOS-Switch", 9600, [
        ("erase startup-config", [(r"\[y/n\]", "y"), (r"Continue.*\?", "y")]),
        ("reload", [(r"save.*\[y/n\]", "n"), (r"continue.*\[y/n\]", "y"), (r"\[y/n\]", "y")]),
    ], "Erases startup configuration and reloads; command and confirmation vary by model/software. Verify prompts before production use."),
    "arubacx": Profile("arubacx", "Aruba CX (AOS-CX)", 115200, [
        ("erase startup-config", [(r"\(y/n\)", "y")]),
        ("boot system", [(r"save.*\(y/n\)", "n"), (r"\(y/n\)", "y")]),
    ], "Model/version-dependent; confirm exact procedure in the matching AOS-CX guide."),
    "juniper": Profile("juniper", "Juniper EX / Junos", 9600, [
        ("request system zeroize", [(r"\[yes,no\]", "yes"), (r"\[yes,no\]", "yes")]),
    ], "Zeroize is destructive and removes configuration and other system data; model/software dependent."),
    "huawei": Profile("huawei", "Huawei VRP", 9600, [
        ("reset saved-configuration", [(r"\[Y/N\]", "y")]),
        ("reboot", [(r"save.*\[Y/N\]", "n"), (r"\[Y/N\]", "y")]),
    ], "VRP prompts and syntax vary; validate on the exact model/release."),
    "openwrt": Profile("openwrt", "OpenWrt (SSH shell only)", 115200, [
        ("firstboot -y", []), ("reboot", []),
    ], "Use only on OpenWrt; firstboot erases overlay configuration. Availability/options vary by release."),
}

class Channel:
    def read(self, n=4096) -> bytes: raise NotImplementedError
    def write(self, data: bytes) -> None: raise NotImplementedError
    def close(self) -> None: raise NotImplementedError

class SerialChannel(Channel):
    def __init__(self, port, baud):
        if serial is None: raise RuntimeError("Missing dependency: install pyserial")
        self.s = serial.Serial(port, baudrate=baud, bytesize=8, parity='N', stopbits=1, timeout=.25, write_timeout=3)
    def read(self, n=4096): return self.s.read(max(1, min(n, self.s.in_waiting or 1)))
    def write(self, data): self.s.write(data); self.s.flush()
    def close(self): self.s.close()

class SSHChannel(Channel):
    def __init__(self, host, port, username, password):
        if paramiko is None: raise RuntimeError("Missing dependency: install paramiko")
        self.client = paramiko.SSHClient()
        self.client.load_system_host_keys()
        # Unknown host keys are rejected by default. Add the device host key to known_hosts first.
        self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        self.client.connect(hostname=host, port=port, username=username, password=password,
                            timeout=10, banner_timeout=10, auth_timeout=15, look_for_keys=False, allow_agent=False)
        self.ch = self.client.invoke_shell(width=200, height=50)
        time.sleep(.6)
    def read(self, n=4096):
        if self.ch.recv_ready(): return self.ch.recv(n)
        time.sleep(.08); return self.ch.recv(n) if self.ch.recv_ready() else b''
    def write(self, data): self.ch.sendall(data)
    def close(self): self.client.close()

def read_until_idle(ch: Channel, timeout=8.0, idle=.6) -> bytes:
    end = time.monotonic() + timeout; last = time.monotonic(); out = bytearray()
    while time.monotonic() < end:
        chunk = ch.read()
        if chunk:
            out.extend(chunk); last = time.monotonic()
            if LOGIN_FAIL.search(out): raise RuntimeError("Authentication failed or access denied")
            if PROMPT.search(out[-500:]) and time.monotonic() - last >= idle: break
        elif out and time.monotonic() - last >= idle:
            break
    return bytes(out)

def send_line(ch, text): ch.write(text.encode("utf-8", "replace") + b"\r\n")

def prepare_cli(ch: Channel, transport: str, platform: str) -> bytes:
    """Wake console and handle ordinary interactive login/enable prompts; never bypass auth."""
    send_line(ch, "")
    out = bytearray()
    deadline = time.monotonic() + 25
    sent_user = sent_pass = sent_enable = False
    username = password = enable_password = None
    while time.monotonic() < deadline:
        chunk = ch.read()
        if chunk:
            out.extend(chunk)
            if LOGIN_FAIL.search(out):
                raise RuntimeError("Authentication failed; no reset command was sent")
            tail = out[-1200:].decode("utf-8", "replace")
            low = tail.lower()
            if re.search(r"(?im)(username|user name|login):\s*$", tail) and not sent_user:
                username = input("Device username: ")
                send_line(ch, username); sent_user = True; continue
            if re.search(r"(?im)password:\s*$", tail) and not sent_pass:
                password = getpass.getpass("Device password: ")
                send_line(ch, password); sent_pass = True; continue
            if platform == "cisco" and re.search(r"(?m)[\w.-]+>\s*$", tail) and not sent_enable:
                send_line(ch, "enable"); sent_enable = True; continue
            if platform == "cisco" and sent_enable and re.search(r"(?im)password:\s*$", tail) and not enable_password:
                enable_password = getpass.getpass("Cisco enable password: ")
                send_line(ch, enable_password); continue
            if re.search(r"(?im)(authentication failed|login incorrect|invalid password|access denied)", tail):
                raise RuntimeError("Authentication failed; no reset command was sent")
            if PROMPT.search(out[-500:]) and len(out) > 0:
                # A username prompt is not a command prompt; don't mistake it for a ready CLI.
                if not re.search(r"(?im)(username|user name|login|password):\s*$", tail):
                    return bytes(out)
        elif out and time.monotonic() + .1 >= deadline:
            break
    if out:
        tail = out[-1000:].decode("utf-8", "replace")
        if re.search(r"(?im)(username|user name|login|password):\s*$", tail):
            raise RuntimeError("Login prompt did not complete; no reset command was sent")
        return bytes(out)
    raise TimeoutError("No CLI output after waking the device; no reset command was sent")

def run_step(ch, command, rules, timeout=30):
    print(f"  -> {command}")
    send_line(ch, command)
    out = bytearray(); deadline = time.monotonic() + timeout; sent = set(); last = time.monotonic()
    while time.monotonic() < deadline:
        chunk = ch.read()
        if chunk:
            out.extend(chunk); last = time.monotonic()
            if LOGIN_FAIL.search(out): raise RuntimeError("Authentication/access failure detected")
            tail = out[-1200:].decode("utf-8", "replace")
            for idx, (pattern, answer) in enumerate(rules):
                if idx not in sent and re.search(pattern, tail, re.I):
                    send_line(ch, answer); sent.add(idx); last = time.monotonic()
                    break
            if re.search(r"(?i)(% ?invalid input|unknown command|unrecognized command|permission denied|not permitted|syntax error)", tail):
                raise RuntimeError(f"Device rejected command {command!r}; response: {tail[-400:]}")
            if PROMPT.search(out[-500:]) and time.monotonic() - last > .8: return tail
        elif out and time.monotonic() - last > 1.2:
            return out.decode("utf-8", "replace")[-1200:]
    if not out: raise TimeoutError(f"No response to command {command!r}")
    return out.decode("utf-8", "replace")[-1200:]

def is_excluded_linux_tty(device):
    """Return True for /dev/tty0..tty59 and /dev/ttyS0..ttyS59.

    tty0..tty59 are Linux virtual consoles. ttyS* devices are conventional
    UARTs and are intentionally omitted here because this workflow expects a
    USB serial adapter (normally ttyUSB* or ttyACM*) for the switch console.
    """
    name = os.path.basename(device)
    match = re.fullmatch(r"ttyS?(\d+)", name)
    return bool(match and 0 <= int(match.group(1)) <= 59)

def choose_port():
    if serial is None: raise RuntimeError("Install pyserial to use serial console")
    # This project targets USB serial console adapters: ttyUSB*, ttyACM*, or
    # Windows COM ports. Do not offer Linux virtual consoles (tty0..tty59) or
    # conventional onboard UARTs (ttyS0..ttyS59) as switch-console adapters.
    ports = [
        p for p in serial.tools.list_ports.comports()
        if not is_excluded_linux_tty(p.device)
        and any(x in p.device for x in ("ttyUSB", "ttyACM", "COM"))
    ]
    if not ports: raise RuntimeError("No suitable USB serial ports found. Linux /dev/tty0..tty59 and /dev/ttyS0..ttyS59 are excluded; check the USB adapter, driver and permissions.")
    for i, p in enumerate(ports, 1): print(f" [{i}] {p.device} — {p.description}")
    while True:
        ans = input("Select port number (q to cancel): ").strip().lower()
        if ans == 'q': return None
        if ans.isdigit() and 1 <= int(ans) <= len(ports): return ports[int(ans)-1].device

BOOT_SUCCESS = {
    "cisco": re.compile(r"(?im)(press return to get started|would you like to enter the initial configuration dialog|\bSwitch[>#]\s*$|\bRouter[>#]\s*$)"),
    "hp": re.compile(r"(?im)(press any key to continue|\b\S+\s*[>#]\s*$)"),
    "arubacx": re.compile(r"(?im)(\bswitch\s+login:\s*$|\bswitch[>#]\s*$|press enter to continue)"),
    "juniper": re.compile(r"(?im)(login:\s*$|amnesiac\s*>|\broot@\S+[>#]\s*$)"),
    "huawei": re.compile(r"(?im)(press enter to get started|<[^>]+>\s*$|\busername:\s*$)"),
    "openwrt": re.compile(r"(?im)(\b\S+ login:\s*$|\broot@\S+:[^#]*#\s*$)"),
}


def init_history_db(db_path: str) -> None:
    """Create the local SQLite reset history database if needed."""
    path = Path(db_path).expanduser()
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS reset_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                platform TEXT NOT NULL,
                hardware_model TEXT NOT NULL,
                software_version TEXT NOT NULL,
                transport TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                result TEXT NOT NULL,
                boot_status TEXT NOT NULL DEFAULT 'not monitored',
                boot_details TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                software_image TEXT NOT NULL DEFAULT '',
                system_serial TEXT NOT NULL DEFAULT ''
            )
        """)
        # Upgrade databases created by earlier script versions without losing history.
        columns = {row[1] for row in db.execute("PRAGMA table_info(reset_history)")}
        if "software_image" not in columns:
            db.execute("ALTER TABLE reset_history ADD COLUMN software_image TEXT NOT NULL DEFAULT ''")
        if "system_serial" not in columns:
            db.execute("ALTER TABLE reset_history ADD COLUMN system_serial TEXT NOT NULL DEFAULT ''")
        db.commit()


def create_history_record(db_path: str, *, platform: str, hardware_model: str,
                          software_version: str, transport: str, endpoint: str,
                          notes: str = "") -> int:
    init_history_db(db_path)
    with sqlite3.connect(Path(db_path).expanduser()) as db:
        cur = db.execute("""
            INSERT INTO reset_history
            (started_at, platform, hardware_model, software_version, transport,
             endpoint, result, boot_status, notes)
            VALUES (?, ?, ?, ?, ?, ?, 'started', 'pending', ?)
        """, (datetime.now().astimezone().isoformat(timespec="seconds"), platform,
              hardware_model, software_version, transport, endpoint, notes))
        db.commit()
        return int(cur.lastrowid)


def update_history_record(db_path: str, record_id: int, *, result: str,
                          boot_status: str, boot_details: str = "") -> None:
    with sqlite3.connect(Path(db_path).expanduser()) as db:
        db.execute("""
            UPDATE reset_history SET finished_at=?, result=?, boot_status=?, boot_details=?
            WHERE id=?
        """, (datetime.now().astimezone().isoformat(timespec="seconds"), result,
              boot_status, boot_details[-8000:], record_id))
        db.commit()


def update_detected_device_facts(db_path: str, record_id: int, facts: dict[str, str]) -> None:
    """Update inventory fields only when a meaningful value was detected from console output."""
    allowed = {"hardware_model", "software_version", "software_image", "system_serial"}
    updates = {key: value.strip() for key, value in facts.items()
               if key in allowed and value and value.strip() and value.strip().lower() != "unknown"}
    if not updates:
        return
    assignments = ", ".join(f"{key}=?" for key in updates)
    with sqlite3.connect(Path(db_path).expanduser()) as db:
        db.execute(f"UPDATE reset_history SET {assignments} WHERE id=?",
                   (*updates.values(), record_id))
        db.commit()


def extract_device_facts(output: str, platform: str) -> dict[str, str]:
    """Extract common model/version/serial identifiers from captured boot-console text."""
    facts: dict[str, str] = {}

    def first(patterns: list[str]) -> str:
        for pattern in patterns:
            match = re.search(pattern, output, re.IGNORECASE | re.MULTILINE)
            if match:
                return match.group(1).strip().strip(",; ")
        return ""

    if platform == "cisco":
        facts["hardware_model"] = first([
            r"^\s*Model number\s*:\s*([A-Z0-9][A-Z0-9-]+)",
            r"^\s*cisco\s+(\S+)\s+\([^\n]+?\)\s+processor",
        ])
        facts["system_serial"] = first([
            r"^\s*System serial number\s*:\s*(\S+)",
            r"^\s*Processor board ID\s+(\S+)",
        ])
        # The detailed IOS software banner is preferred; the version table is a fallback.
        facts["software_version"] = first([
            r"\bVersion\s+([0-9][^,\s]+)",
            r"^\s*\*?\s*\d+\s+\d+\s+\S+\s+([0-9][^\s]+)\s+\S+\s*$",
        ])
        facts["software_image"] = first([
            r"^\s*\*?\s*\d+\s+\d+\s+\S+\s+[0-9][^\s]+\s+(\S+)\s*$",
            r"Cisco IOS Software,\s*([^,]+)",
        ])
    elif platform == "arubacx":
        facts["hardware_model"] = first([r"^\s*Product Name\s*:\s*(.+?)\s*$", r"^\s*SKU\s*:\s*(\S+)"])
        facts["software_version"] = first([r"^\s*Software Version\s*:\s*(\S+)", r"^\s*Version\s*:\s*(\S+)"])
        facts["system_serial"] = first([r"^\s*Serial Number\s*:\s*(\S+)"])
    elif platform == "hp":
        facts["hardware_model"] = first([r"^\s*Product(?:\s+Name)?\s*:\s*(.+?)\s*$", r"^\s*HP\s+(\S+)"])
        facts["software_version"] = first([r"^\s*Software revision\s*[: ]\s*(\S+)", r"^\s*Software Version\s*:\s*(\S+)"])
        facts["system_serial"] = first([r"^\s*Serial Number\s*:\s*(\S+)"])
    elif platform == "juniper":
        facts["hardware_model"] = first([r"^Model:\s*(\S+)", r"^Hardware model:\s*(\S+)"])
        facts["software_version"] = first([r"^Junos:\s*(\S+)", r"JUNOS Software Release \[([^]]+)\]"])
        facts["system_serial"] = first([r"^Chassis serial number\s*:\s*(\S+)"])
    elif platform == "huawei":
        facts["hardware_model"] = first([r"^HUAWEI\s+(\S+)", r"^VRP.*?\b(\S+)"])
        facts["software_version"] = first([r"VRP.*?Version\s+([^,\s]+)", r"Version\s+([^,\s]+)"])
        facts["system_serial"] = first([r"^ESN\s*:\s*(\S+)", r"^Serial number\s*:\s*(\S+)"])
    elif platform == "openwrt":
        facts["hardware_model"] = first([r"^board_name=(.+?)\s*$", r"^DISTRIB_TARGET=(.+?)\s*$"])
        facts["software_version"] = first([r"^DISTRIB_RELEASE='?([^'\s]+)", r"^DISTRIB_REVISION='?([^'\s]+)"])

    return {key: value for key, value in facts.items() if value}


def print_history(db_path: str, limit: int = 50) -> None:
    path = Path(db_path).expanduser()
    if not path.exists():
        print(f"No history database found at {path}")
        return
    with sqlite3.connect(path) as db:
        rows = db.execute("""
            SELECT id, started_at, platform, hardware_model, software_version,
                   software_image, system_serial, transport, endpoint, result, boot_status
            FROM reset_history ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
    if not rows:
        print("Reset history database is empty.")
        return
    print(f"Reset history (latest {len(rows)} record(s), database: {path})")
    print("ID | Date/time | Platform | Hardware | Software | Image | System serial | Transport/endpoint | Reset result | Boot status")
    print("-" * 130)
    for row in rows:
        vals = [str(v or "") for v in row]
        print(" | ".join(v.replace("\n", " ") for v in vals))


def monitor_serial_boot(ch: Channel, platform: str, timeout: int = 300) -> tuple[str, str, str]:
    """Display serial boot output until a likely CLI/login-ready marker or timeout."""
    print(f"\nBoot monitor: watching serial console for up to {timeout} seconds.")
    print("Boot output follows; Ctrl-C stops monitoring but does not interrupt the switch boot.\n")
    matcher = BOOT_SUCCESS.get(platform, re.compile(r"(?im)(login:\s*$|[>#]\s*$)"))
    start = time.monotonic()
    output = bytearray()
    last_data = start
    try:
        while time.monotonic() - start < timeout:
            chunk = ch.read(4096)
            if chunk:
                output.extend(chunk)
                last_data = time.monotonic()
                decoded = chunk.decode("utf-8", "replace")
                print(decoded, end="", flush=True)
                recent = output[-12000:].decode("utf-8", "replace")
                if matcher.search(recent):
                    return "console-ready-marker", "Recognized a possible boot-complete/login/CLI marker in console output. Verify the displayed prompt and device state.", output.decode("utf-8", "replace")
                if re.search(r"(?im)(kernel panic|boot failed|unable to boot|fatal error|filesystem.*corrupt|failed to start)" , recent):
                    return "boot-error-detected", "Console output contains a possible boot failure marker; inspect the console output.", output.decode("utf-8", "replace")
            else:
                time.sleep(.1)
    except KeyboardInterrupt:
        return "monitor-interrupted", "Monitoring stopped by user; this does not interrupt the device boot.", output.decode("utf-8", "replace")
    if output:
        return "timeout-with-output", f"No recognized boot-complete marker within {timeout}s; last console data {int(time.monotonic()-last_data)}s ago.", output.decode("utf-8", "replace")
    return "timeout-no-output", f"No console output received within {timeout}s. Check console cabling, baud rate, and device power.", ""


def monitor_ssh_boot(host: str, port: int, timeout: int = 300) -> tuple[str, str]:
    """Wait for SSH TCP to go down and return; this checks reachability, not CLI health."""
    print(f"\nBoot monitor: checking {host}:{port} for a reboot (up to {timeout}s).")
    print("SSH reachability alone cannot prove that the switch has completed booting.")
    start = time.monotonic()
    saw_down = False
    was_up = False
    try:
        while time.monotonic() - start < timeout:
            reachable = False
            try:
                with socket.create_connection((host, port), timeout=2):
                    reachable = True
            except OSError:
                reachable = False
            if reachable:
                print("SSH TCP port reachable.")
                if saw_down:
                    return "ssh-port-returned", "SSH TCP port returned after an observed outage; authentication and CLI readiness were not verified."
                was_up = True
            else:
                print("SSH TCP port not reachable.")
                if was_up:
                    saw_down = True
            time.sleep(3)
    except KeyboardInterrupt:
        return "monitor-interrupted", "Monitoring stopped by user."
    if not saw_down:
        return "reboot-not-observed", "SSH outage followed by recovery was not observed; reboot/boot completion cannot be confirmed over this check."
    return "ssh-not-returned", f"SSH did not return within {timeout}s after an outage."


def main():
    ap = argparse.ArgumentParser(
        description="Authenticated switch reset over serial or SSH; manual recovery guidance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Inspired by the original project:\n"
            "  https://github.com/dev-sasa/switch-reset\n\n"
            "Fork and further development:\n"
            "  https://github.com/mats-nk/switch-reset"
        ),
    )
    ap.add_argument("--transport", choices=["serial", "ssh"], help="Connection method; interactive menu if omitted")
    ap.add_argument("--platform", choices=sorted(PROFILES), help="Platform profile; interactive menu if omitted")
    ap.add_argument("--host", help="SSH hostname/IP")
    ap.add_argument("--port", type=int, help="SSH port (default 22) or serial port selection is interactive")
    ap.add_argument("--database", default="switch_reset_history.sqlite3", help="SQLite reset history path (default: ./switch_reset_history.sqlite3)")
    ap.add_argument("--list-history", action="store_true", help="List recent reset records and exit")
    ap.add_argument("--boot-monitor-timeout", type=int, default=300, help="Maximum boot-monitor duration in seconds (default: 300)")
    ap.add_argument("--recovery-guide", action="store_true", help="Show documented/manual recovery guidance; performs no changes")
    ap.add_argument("--bootloader-guide", choices=["cisco-cat9k", "cisco-legacy", "hp-procurve-1700-1800"], help="Show a model-specific bootloader/factory-reset guide; performs no changes")
    args = ap.parse_args()
    print("Switch Reset v2 — destructive operations require explicit confirmation")
    if args.list_history:
        print_history(args.database)
        return 0
    if args.recovery_guide:
        print_recovery_guidance(args.platform); return 0
    if args.bootloader_guide:
        print_bootloader_guide(args.bootloader_guide); return 0
    transport = args.transport or input("Connection [1] serial console [2] SSH: ").strip()
    transport = {"1":"serial", "2":"ssh"}.get(transport, transport)
    if transport not in ("serial", "ssh"): raise RuntimeError("Choose serial or ssh")
    platform = args.platform or choose_platform()
    profile = PROFILES[platform]
    if transport == "serial":
        port = choose_port()
        if not port: return 0
        ch = SerialChannel(port, profile.baud)
        endpoint = f"serial {port} @ {profile.baud} baud"
    else:
        host = args.host or input("Device hostname/IP: ").strip()
        if not host: raise RuntimeError("SSH host is required")
        port = args.port or 22
        username = input("Username: ")
        password = getpass.getpass("Password: ")
        ch = SSHChannel(host, port, username, password)
        endpoint = f"SSH {username}@{host}:{port}"
    try:
        # SSH authentication occurs before shell creation; serial may need device CLI login.
        banner = prepare_cli(ch, transport, platform)
        print("Connected. Initial response:")
        print(banner.decode("utf-8", "replace")[-800:])
        print("\nSelected profile:", profile.name)
        print("Endpoint:", endpoint)
        print("Effects:", profile.notes)
        print("\nWARNING: This operation can disconnect the device and erase its configuration.")
        hardware_model = input("Hardware model / product ID (exact model if known; Enter = unknown): ").strip() or "unknown"
        software_version = input("Software / firmware version (Enter = unknown): ").strip() or "unknown"
        notes = input("Notes (optional; e.g. asset ID, reason, backup reference): ").strip()
        expected = "RESET " + profile.key.upper()
        if input(f"Type exactly {expected!r} to proceed: ").strip() != expected:
            print("Cancelled; no reset command sent."); return 0
        record_id = create_history_record(args.database, platform=platform,
            hardware_model=hardware_model, software_version=software_version,
            transport=transport, endpoint=endpoint, notes=notes)
        print(f"Reset history record #{record_id} started in {Path(args.database).expanduser()}")
        reset_result = "commands-failed"
        boot_status = "not monitored"
        boot_details = ""
        try:
            for cmd, rules in profile.commands:
                response = run_step(ch, cmd, rules)
                if response.strip(): print(response[-500:])
            reset_result = "reset-commands-sent"
        except Exception as exc:
            boot_details = f"Reset command error: {exc}"
            update_history_record(args.database, record_id, result=reset_result,
                                  boot_status="not started", boot_details=boot_details)
            raise
        finally:
            # A reset/reload may terminate the CLI. Close the current channel before monitoring SSH.
            if transport == "ssh":
                try: ch.close()
                except Exception: pass
        console_output = ""
        if transport == "serial":
            boot_status, boot_details, console_output = monitor_serial_boot(ch, platform, max(15, args.boot_monitor_timeout))
            detected = extract_device_facts(console_output, platform)
            if detected:
                update_detected_device_facts(args.database, record_id, detected)
                print("\nDevice facts detected from boot console:")
                for key, value in detected.items():
                    print(f"  {key}: {value}")
            else:
                print("\nNo hardware/software facts were recognized in captured console output; existing inventory fields were preserved.")
        else:
            boot_status, boot_details = monitor_ssh_boot(host, port, max(15, args.boot_monitor_timeout))
            boot_details += " Device facts were not auto-detected because SSH monitoring checks TCP reachability only."
        update_history_record(args.database, record_id, result=reset_result,
                              boot_status=boot_status, boot_details=boot_details)
        print(f"\nBoot monitor result: {boot_status}")
        print(boot_details)
        print(f"History saved to: {Path(args.database).expanduser()}")
    finally:
        ch.close()
    return 0

def choose_platform():
    keys = list(PROFILES)
    for i, k in enumerate(keys, 1): print(f" [{i}] {PROFILES[k].name} ({k})")
    while True:
        v = input("Select platform number: ").strip()
        if v.isdigit() and 1 <= int(v) <= len(keys): return keys[int(v)-1]

def print_recovery_guidance(platform: Optional[str]):
    guides = {
      "cisco": [
        "Catalyst bootloader procedures are model/IOS-specific. Use the dedicated --bootloader-guide cisco-cat9k or cisco-legacy guide only for the matching family.",
        "Password recovery and factory reset are different: SWITCH_IGNORE_STARTUP_CFG=1 is normally a password-recovery mechanism and does not itself erase the saved configuration.",
        "Cisco Catalyst 9000 ROMMON guide: https://www.cisco.com/c/en/us/support/docs/switches/catalyst-9300-series-switches/216944-troubleshoot-bootloader-rommon-and-pas.html",
        "Cisco legacy Catalyst guide: https://www.cisco.com/c/en/us/support/docs/switches/catalyst-2950-series-switches/12040-pswdrec-2900xl.html",
      ],
      "hp": [
        "Most ProCurve/AOS-Switch factory reset procedures are model-specific CLI or physical Reset/Clear procedures, not a generic ROMMON sequence.",
        "HPE ProCurve 2520 factory default: https://support.hpe.com/hpesc/public/docDisplay?docId=c01983148&docLocale=en_US",
        "AOS-Switch factory-default procedure: https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/MCG/WC/content/common%20files/res-fac-def-con.htm",
        "Front-panel factory reset may be disabled by configuration: https://arubanetworking.hpe.com/techdocs/AOS-S/16.11/MCG/KB/content/kb/fro-pan-sec-pas-cle.htm",
      ],
      "arubacx": ["Use the exact AOS-CX model and software guide. Do not apply AOS-Switch/ProCurve procedures to AOS-CX without verification."],
      "juniper": ["Junos recovery and factory reset differ by platform and boot media. Consult the exact EX model's Juniper documentation; this tool does not automate loader commands."],
      "huawei": ["Huawei BootROM/BootLoader recovery varies by model/release and may be restricted. Consult the exact model's official configuration/recovery guide."],
      "openwrt": ["OpenWrt failsafe/reset depends on hardware, button wiring, and release. Use the exact device page in the OpenWrt wiki; do not assume generic router reset timing."],
    }
    keys = [platform] if platform else list(guides)
    for key in keys:
        if key not in guides: continue
        print(f"\n{PROFILES.get(key, Profile(key,key,0,[],'' )).name} — recovery references")
        for line in guides[key]: print("- " + line)
    print("\nNo recovery commands were sent. Confirm exact model, hardware revision, software version, ownership, and configuration-backup status before proceeding.")

def print_bootloader_guide(guide: str):
    """Print documented, model-scoped recovery steps; never runs them automatically."""
    guides = {
      "cisco-cat9k": {
        "title": "Cisco Catalyst 9200/9200L, 9300/9300L, 9400, 9500, 9600 — ROMMON",
        "url": "https://www.cisco.com/c/en/us/support/docs/switches/catalyst-9300-series-switches/216944-troubleshoot-bootloader-rommon-and-pas.html",
        "steps": [
          "Confirm the exact model, IOS XE release, and whether this is standalone, StackWise, StackWise Virtual, or a modular chassis. Stack/supervisor procedures differ.",
          "Connect to the physical console. Power-cycle the switch and interrupt boot using the model's documented Ctrl-C/Mode-button method until the documented switch:/rommon prompt appears.",
          "If the console explicitly asks whether to reset the system to the default configuration because password recovery is disabled, answering y is the documented factory-default path for the documented Catalyst 9000 cases. This is destructive; do not answer until you intend to erase the configuration.",
          "Otherwise, do not mistake SWITCH_IGNORE_STARTUP_CFG=1 for a factory reset. Cisco documents it as a way to bypass startup configuration for password recovery; it can preserve the saved configuration.",
          "Do not issue guessed erase, format, or boot commands. If the default-reset prompt does not appear, stop and follow the exact model/release guide linked above. This tool does not automatically type ROMMON commands.",
          "After reset, wait for the normal boot and verify the device starts with its default configuration. Reconfigure management access securely before reconnecting it to a production network.",
        ],
      },
      "cisco-legacy": {
        "title": "Legacy Cisco Catalyst 2900XL/3500XL/2940/2950/2955/2960/2970/3550/3560/3750 families",
        "url": "https://www.cisco.com/c/en/us/support/docs/switches/catalyst-2950-series-switches/12040-pswdrec-2900xl.html",
        "steps": [
          "Use this guide only for the listed legacy families; it is not for Catalyst 9000.",
          "Connect to the console at the model-documented settings (the referenced guide specifies 9600 baud, 8 data bits, no parity, 1 stop bit).",
          "Power off, hold the Mode button while restoring power, and release it according to the exact model's LED/prompt instructions until the documented switch: bootloader prompt appears.",
          "The result depends on the password-recovery setting and model. Cisco documents that when password recovery is disabled, interrupting boot can require agreeing to return the switch to default and delete config.text and vlan.dat. Confirm the exact model's guide before accepting any destructive prompt.",
          "Do not apply Catalyst 9000 ROMMON variables to these legacy platforms. If the expected prompt differs, stop rather than guessing bootloader commands.",
        ],
      },
      "hp-procurve-1700-1800": {
        "title": "HP ProCurve 1700/1800 series — documented physical factory reset (not ROMMON)",
        "url": "https://support.hpe.com/hpesc/public/docDisplay?docId=c01752898&docLocale=en_US",
        "steps": [
          "This procedure applies only to ProCurve 1700-8/1700-24/1800-8/1800-24. It is not a general ProCurve bootloader sequence.",
          "Remove power. Connect front-panel port 1 to port 2 with an Ethernet cable. Restore power and wait at least 40 seconds before disconnecting the loop cable.",
          "HPE documents that this restores factory defaults, removes the password, and resets the IP address to 192.168.2.10. Firmware-specific caveats are listed in the official guide.",
          "Do not use this procedure on other ProCurve/Aruba models. For other families, use the exact model's documentation; front-panel reset may be disabled by security settings.",
        ],
      },
    }
    item = guides[guide]
    print("\n" + item["title"])
    print("Official reference: " + item["url"])
    print("\nThis is a guided reference only. The script will not reboot the device, type bootloader commands, or erase data.\n")
    for i, step in enumerate(item["steps"], 1):
        print(f"{i}. {step}")
    print("\nFactory reset is destructive. Save a configuration backup where possible and disconnect production links before proceeding.")

if __name__ == "__main__":
    try: sys.exit(main())
    except KeyboardInterrupt: print("\nCancelled by user.", file=sys.stderr); sys.exit(130)
    except Exception as exc: print(f"ERROR: {exc}", file=sys.stderr); sys.exit(1)
