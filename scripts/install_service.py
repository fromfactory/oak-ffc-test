#!/usr/bin/env python3
"""Install the web app and its existing Wi-Fi hotspot for unattended Pi startup."""

import argparse
import configparser
import grp
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

if __package__:
    from . import hotspot
else:
    import hotspot


PROJECT = Path(__file__).resolve().parent.parent
UNIT_NAME = "oak-ffc-test.service"
UNIT_PATH = Path("/etc/systemd/system") / UNIT_NAME
SYSTEMD_RUNTIME = Path("/run/systemd/system")
MARKER = "# Managed by oak-ffc-test scripts/install_service.py"


class ServiceError(Exception):
    """A setup failure with an actionable explanation."""


def validate_value(value):
    value = str(value)
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ServiceError("Service paths and account names must contain no control characters.")
    return value


def quote(value, *, environment=False):
    value = validate_value(value)
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if environment:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def render_unit(project, user, group, port, capture_dir, demo=False):
    """Format raw directives and quoted ExecStart arguments without a shell.

    WorkingDirectory does not unquote values. Its '/.' suffix preserves any
    trailing space through the unit file's line parser.
    """
    if any(char in str(project) for char in ('"', "\\")):
        raise ServiceError("systemd cannot execute a Python path containing a quote or backslash. "
                           "Move the project to a directory without those characters and retry.")
    arguments = [Path(project) / ".venv/bin/python", "-m", "oak_camera", "--host", "0.0.0.0",
                 "--port", str(port), "--capture-dir", capture_dir]
    if demo:
        arguments.append("--demo")
    return f"""{MARKER}
[Unit]
Description=OAK FFC TEST camera web app
Wants=NetworkManager.service
After=NetworkManager.service
StartLimitIntervalSec=0

[Service]
Type=exec
User={validate_value(user).replace('%', '%%')}
Group={validate_value(group).replace('%', '%%')}
WorkingDirectory={validate_value(project).replace('%', '%%')}/.
ExecStart={quote(arguments[0])} {' '.join(quote(argument, environment=True) for argument in arguments[1:])}
Restart=on-failure
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=45

[Install]
WantedBy=multi-user.target
"""


def systemctl(*arguments, check=True, timeout=60):
    try:
        result = subprocess.run(["systemctl", *arguments], capture_output=True, text=True,
                                timeout=timeout, check=False, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(f"Cannot run systemctl: {exc}") from exc
    if check and result.returncode:
        raise ServiceError(result.stderr.strip() or f"systemctl {' '.join(arguments)} failed.")
    return result


def owned_unit():
    if UNIT_PATH.is_symlink() or (UNIT_PATH.exists() and not UNIT_PATH.is_file()):
        raise ServiceError(f"Refusing to replace a symbolic link or non-file at {UNIT_PATH}.")
    if not UNIT_PATH.exists():
        return None
    content = UNIT_PATH.read_bytes()
    if content.splitlines()[:1] != [MARKER.encode()]:
        raise ServiceError(f"An unmanaged unit exists at {UNIT_PATH}. Back it up and remove it locally "
                           "before using this installer.")
    return content


def atomic_write(content):
    descriptor, filename = tempfile.mkstemp(prefix=".oak-ffc-service-", dir=UNIT_PATH.parent)
    try:
        os.fchmod(descriptor, 0o644)
        os.fchown(descriptor, 0, 0)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(filename, UNIT_PATH)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if os.path.exists(filename):
            os.unlink(filename)


def preflight(args):
    if os.geteuid() != 0:
        raise ServiceError("Run this installer with sudo python3 scripts/install_service.py.")
    if not args.user or args.user == "root":
        raise ServiceError("Use sudo from your normal account, or specify --user YOUR_NORMAL_USER.")
    try:
        account = pwd.getpwnam(args.user)
        group = grp.getgrgid(account.pw_gid).gr_name
    except KeyError as exc:
        raise ServiceError("--user must name an existing ordinary Linux account.") from exc
    if account.pw_uid == 0:
        raise ServiceError("The app must run as an ordinary account, not root.")
    if not 1024 <= args.port <= 65535:
        raise ServiceError("--port must be between 1024 and 65535 for an ordinary account.")
    quote(args.capture_dir)
    capture_dir = (PROJECT / args.capture_dir).resolve()
    unit = render_unit(PROJECT, account.pw_name, group, args.port, capture_dir, args.demo).encode()
    previous = owned_unit()  # Reject conflicting units before touching networking.
    python = PROJECT / ".venv/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ServiceError("Missing .venv/bin/python. Run bash scripts/install.sh as your normal user first.")
    if not shutil.which("systemctl") or not SYSTEMD_RUNTIME.is_dir():
        raise ServiceError("This installer requires Linux running systemd.")
    # Check imports and storage access using the same account and groups as the service.
    probe = ("import os,sys; from pathlib import Path; "
             "import flask,depthai,numpy,cv2,PIL,oak_camera; p=Path(sys.argv[1]); "
             "p=next(x for x in (p,*p.parents) if x.exists()); "
             "assert p.is_dir() and os.access(p,os.W_OK|os.X_OK), 'Capture directory is not writable'")
    try:
        result = subprocess.run([str(python), "-c", probe, str(capture_dir)], cwd=PROJECT,
                                user=account.pw_uid, group=account.pw_gid,
                                extra_groups=os.getgrouplist(account.pw_name, account.pw_gid),
                                capture_output=True, text=True, timeout=30, check=False,
                                env={**os.environ, "DEPTHAI_DISABLE_CRASHDUMP_COLLECTION": "1",
                                     "DEPTHAI_ENABLE_ANALYTICS_COLLECTION": ""})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(f"Cannot validate the app as {args.user}: {exc}") from exc
    if result.returncode:
        raise ServiceError(f"App dependencies or capture access failed for {args.user}. "
                           f"Run bash scripts/install.sh as that user and check capture permissions.\n{result.stderr.strip()}")
    active = systemctl("is-active", "--quiet", UNIT_NAME, check=False).returncode == 0
    enabled = systemctl("is-enabled", "--quiet", UNIT_NAME, check=False).returncode == 0
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("0.0.0.0", args.port))
    except OSError as exc:
        if not (active and previous and f'"--port" "{args.port}"'.encode() in previous):
            raise ServiceError(f"Port {args.port} is occupied. Stop the manually launched app with "
                               "Ctrl+C in its terminal, or choose another --port.") from exc
    if args.password is not None:
        hotspot.validate_password(args.password)
    if args.password_file is not None:
        hotspot.read_password(args.password_file)
    hotspot.check_manager()
    loaded = hotspot.profile_inventory()
    saved = hotspot.owned_file()
    if loaded and saved is None:
        raise ServiceError("The hotspot UUID exists outside the managed keyfile; refusing to change it.")
    if saved:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(saved.decode())
        interface = parser.get("connection", "interface-name")
    else:
        interface = "wlan0"
    hotspot.check_wifi(interface)
    return unit, previous, active, enabled, saved, loaded


def setup_hotspot(args, saved, loaded):
    if saved and not loaded:
        hotspot.nmcli("connection", "load", str(hotspot.KEYFILE))
    interface, ssid, address = "wlan0", "OAK-FFC-TEST", "10.42.0.1/24"
    if saved:
        interface, ssid, address, _ = hotspot.profile_settings()
    if not saved or args.password is not None or args.password_file is not None:
        if hotspot.CONNECTION_UUID in hotspot.nmcli(
                "--get-values", "UUID", "connection", "show", "--active").splitlines():
            hotspot.nmcli("connection", "down", "uuid", hotspot.CONNECTION_UUID)
        hotspot.configure(argparse.Namespace(ssid=ssid, interface=interface, address=address,
                                            password=args.password, password_file=args.password_file,
                                            port=args.port))
    systemctl("enable", "NetworkManager.service")
    hotspot.nmcli("radio", "wifi", "on")
    # Radio unblocking can briefly report unavailable; nmcli up waits for activation.
    hotspot.nmcli("connection", "modify", "uuid", hotspot.CONNECTION_UUID,
                  "connection.autoconnect", "yes", "connection.autoconnect-priority", "999",
                  "connection.autoconnect-retries", "0")
    hotspot.nmcli("connection", "up", "uuid", hotspot.CONNECTION_UUID)
    return ssid, address


def wait_ready(port, timeout=30):
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        active = systemctl("is-active", "--quiet", UNIT_NAME, check=False,
                           timeout=min(2, remaining)).returncode == 0
        if active:
            try:
                if time.monotonic() >= deadline:
                    break
                with opener.open(f"http://127.0.0.1:{port}/api/status",
                                 timeout=min(2, max(0.1, deadline - time.monotonic()))) as response:
                    status = json.load(response)
                if isinstance(status, dict) and "cameras" in status:
                    # Recheck after the response; a failing service must not be mistaken for an old app.
                    remaining = deadline - time.monotonic()
                    if remaining > 0 and systemctl("is-active", "--quiet", UNIT_NAME, check=False,
                                                   timeout=min(2, remaining)).returncode == 0:
                        return
            except (OSError, urllib.error.URLError, ValueError):
                pass
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise ServiceError("The service did not become ready within 30 seconds. Check "
                       f"sudo journalctl -u {UNIT_NAME} -n 50 --no-pager and stop any old manually "
                       "launched app before retrying.")


def install(args):
    unit, previous, active, enabled, saved, loaded = preflight(args)
    try:
        ssid, address = setup_hotspot(args, saved, loaded)
    except (hotspot.HotspotError, ServiceError, OSError) as exc:
        raise ServiceError(f"{exc}\nHotspot setup was partially applied; any configured password remains "
                           "saved. The app service has not been changed. Check hotspot.py status and retry.") from exc
    try:
        atomic_write(unit)
        systemctl("daemon-reload")
        systemctl("enable", UNIT_NAME)
        systemctl("restart", UNIT_NAME)
        wait_ready(args.port)
    except (ServiceError, OSError) as exc:
        try:
            systemctl("stop", UNIT_NAME)
            if not enabled:
                systemctl("disable", UNIT_NAME)
            if previous is None:
                UNIT_PATH.unlink(missing_ok=True)
            else:
                atomic_write(previous)
            systemctl("daemon-reload")
            if active:
                systemctl("restart", UNIT_NAME)
        except (ServiceError, OSError) as rollback:
            raise ServiceError(f"{exc}\nService rollback failed: {rollback}. Hotspot settings remain saved.") from exc
        raise ServiceError(f"{exc}\nPrevious service configuration restored. Hotspot settings remain saved.") from exc
    print(f"Installed and running: {UNIT_NAME}\nSSID: {ssid}\nBrowser URL: http://{address.split('/')[0]}:{args.port}")
    print("Future use: power on the Pi, connect to this Wi-Fi, and open the browser URL.")
    print(f"Status: sudo systemctl status {UNIT_NAME}\nLogs: sudo journalctl -u {UNIT_NAME} -n 50 --no-pager")
    print(f"Stop: sudo systemctl stop {UNIT_NAME}\nDisable app at boot: sudo systemctl disable --now {UNIT_NAME}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=os.environ.get("SUDO_USER"), help="Ordinary app account (default: sudo caller)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--capture-dir", default="captures", help="Capture path, relative to the project or absolute")
    parser.add_argument("--demo", action="store_true", help="Explicitly use synthetic cameras")
    passwords = parser.add_mutually_exclusive_group()
    passwords.add_argument("--password", help="Set a fixed hotspot passphrase; omit to keep the saved password")
    passwords.add_argument("--password-file", type=Path, help="Read a fixed hotspot passphrase from a file")
    args = parser.parse_args(argv)
    try:
        install(args)
    except (ServiceError, hotspot.HotspotError, OSError, configparser.Error) as exc:
        print(f"Service setup error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
