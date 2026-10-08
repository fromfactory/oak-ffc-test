#!/usr/bin/env python3
"""Stage and control an OAK FFC hotspot using Raspberry Pi OS NetworkManager."""

import argparse
import configparser
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile


CONNECTION_ID = "oak-ffc-hotspot"
CONNECTION_UUID = "046c3d51-9d69-4623-91a8-00d341ac3280"
KEYFILE = Path("/etc/NetworkManager/system-connections/oak-ffc-hotspot.nmconnection")
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(net) for net in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


class HotspotError(Exception):
    """An actionable setup or NetworkManager error."""


def nmcli(*args):
    """Run only explicit commands; no credentials are passed in arguments."""
    try:
        result = subprocess.run(
            ["nmcli", "--wait", "30", "--colors", "no", *args],
            capture_output=True, text=True, timeout=40,
            env={**os.environ, "LC_ALL": "C"}, check=False,
        )
    except FileNotFoundError as exc:
        raise HotspotError("nmcli is missing. Use Raspberry Pi OS with NetworkManager installed.") from exc
    except subprocess.TimeoutExpired as exc:
        raise HotspotError("NetworkManager did not respond in time; check its service and retry.") from exc
    if result.returncode:
        raise HotspotError(result.stderr.strip() or "NetworkManager command failed.")
    return result.stdout.strip()


def require_root():
    if os.geteuid() != 0:
        raise HotspotError("This command requires root. Run it with sudo python3 scripts/hotspot.py.")


def check_manager():
    if nmcli("--get-values", "RUNNING", "general") != "running":
        raise HotspotError("NetworkManager is not running; start its service before configuring the hotspot.")


def check_wifi(interface, activating=False):
    fields = "GENERAL.TYPE,GENERAL.NM-MANAGED,WIFI-PROPERTIES.AP,GENERAL.STATE"
    values = nmcli("--get-values", fields, "device", "show", interface).splitlines()
    if len(values) != 4:
        raise HotspotError(f"Cannot inspect Wi-Fi interface {interface} with NetworkManager.")
    kind, managed, ap, state = values
    if kind != "wifi" or ap != "yes":
        raise HotspotError(f"{interface} must be a Wi-Fi interface supporting access-point (AP) mode.")
    if managed != "yes":
        raise HotspotError(f"{interface} is unmanaged. Configure it for NetworkManager before retrying.")
    if activating and (state.split()[0] in {"10", "20"} or nmcli("radio", "wifi") != "enabled"):
        raise HotspotError(
            f"{interface} is unavailable or Wi-Fi is disabled. Check rfkill and the WLAN country "
            "in raspi-config; enable Wi-Fi with sudo nmcli radio wifi on, then retry."
        )
    # Configuration can be staged while the radio is disabled; activation is explicit.
    if not (shutil.which("dnsmasq") or Path("/usr/sbin/dnsmasq").is_file()):
        raise HotspotError("DHCP requires dnsmasq. Install it with sudo apt install dnsmasq-base, then retry.")


def validate_config(ssid, interface, address):
    if not 1 <= len(ssid.encode("utf-8")) <= 32 or not ssid.isprintable():
        raise HotspotError("SSID must contain 1–32 UTF-8 bytes without control characters.")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,14}", interface):
        raise HotspotError("Interface must be a valid Linux interface name of 1–15 characters.")
    try:
        ip = ipaddress.IPv4Interface(address)
    except ValueError as exc:
        raise HotspotError("Address must be a private IPv4 host address with a /24 prefix.") from exc
    if (ip.network.prefixlen != 24 or not any(ip.ip in net for net in PRIVATE_NETWORKS)
            or ip.ip in {ip.network.network_address, ip.network.broadcast_address}):
        raise HotspotError("Address must be a usable private IPv4 host address with a /24 prefix.")
    return str(ip)


def validate_password(password):
    if not 8 <= len(password) <= 63 or any(not 32 <= ord(c) <= 126 for c in password):
        raise HotspotError("Password must contain 8–63 printable ASCII characters on one line.")
    return password


def read_password(path):
    if path is None:
        return secrets.token_urlsafe(18), True
    try:
        with path.open(encoding="ascii") as handle:
            password = handle.read(66).removesuffix("\n").removesuffix("\r")
    except UnicodeError as exc:
        raise HotspotError("Password file must contain an ASCII WPA2 passphrase.") from exc
    return validate_password(password), False


def profile_inventory():
    profiles = {}
    for line in nmcli("--terse", "--escape", "no", "--fields", "UUID,NAME", "connection", "show").splitlines():
        uuid, separator, name = line.partition(":")
        if separator:
            profiles[uuid] = name
    if any(name == CONNECTION_ID and uuid != CONNECTION_UUID for uuid, name in profiles.items()):
        raise HotspotError(f"An unrelated connection already uses {CONNECTION_ID}; rename it manually first.")
    if CONNECTION_UUID in profiles and profiles[CONNECTION_UUID] != CONNECTION_ID:
        raise HotspotError("An unrelated connection uses the hotspot UUID; refusing to change it.")
    return CONNECTION_UUID in profiles


def owned_file():
    if KEYFILE.is_symlink():
        raise HotspotError(f"Refusing to replace a symbolic link at {KEYFILE}.")
    if not KEYFILE.exists():
        return None
    content = KEYFILE.read_bytes()
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(content.decode("utf-8"))
        if (parser.get("connection", "id") != CONNECTION_ID
                or parser.get("connection", "uuid") != CONNECTION_UUID):
            raise ValueError("ownership mismatch")
    except (ValueError, configparser.Error) as exc:
        raise HotspotError(f"An unrelated or invalid keyfile exists at {KEYFILE}; refusing to replace it.") from exc
    return content


def atomic_write(content):
    descriptor, filename = tempfile.mkstemp(prefix=".oak-ffc-hotspot-", dir=KEYFILE.parent)
    try:
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, 0, 0)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(filename, KEYFILE)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if os.path.exists(filename):
            os.unlink(filename)


def saved_psk(content, ssid, loaded):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(content.decode("utf-8"))
    psk = parser.get("wifi-security", "psk", fallback="")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", psk):
        raise HotspotError("Saved hotspot password is invalid. Supply --password or --password-file to set it.")
    if loaded:
        # NetworkManager can rewrite SSID byte lists as escaped text on start/stop.
        # Read the decoded name from the loaded profile rather than interpreting it.
        same_ssid = profile_settings()[1] == ssid
    else:
        stored_ssid = parser.get("wifi", "ssid", fallback="")
        if re.fullmatch(r"(?:[0-9]{1,3};)+", stored_ssid):
            try:
                same_ssid = bytes(int(byte) for byte in stored_ssid.split(";")[:-1]) == ssid.encode("utf-8")
            except ValueError:
                same_ssid = False
        elif "\\" in stored_ssid:
            raise HotspotError("Load the saved hotspot profile or supply --password or --password-file to reconfigure it.")
        else:
            same_ssid = stored_ssid == ssid
    if not same_ssid:
        raise HotspotError("Changing the SSID requires --password or --password-file, even to keep the same password.")
    return psk


def render_keyfile(ssid, interface, address, password=None, *, psk=None):
    # NetworkManager accepts the documented decimal-byte SSID representation.
    # A derived 64-digit PSK avoids quoting arbitrary passphrases in GLib keyfiles.
    ssid_bytes = ssid.encode("utf-8")
    ssid_value = "".join(f"{byte};" for byte in ssid_bytes)
    if psk is None:
        psk = hashlib.pbkdf2_hmac("sha1", password.encode("ascii"), ssid_bytes, 4096, 32).hex()
    return f"""[connection]
id={CONNECTION_ID}
uuid={CONNECTION_UUID}
type=wifi
interface-name={interface}
autoconnect=false
autoconnect-priority=999

[wifi]
mode=ap
band=bg
ssid={ssid_value}
security=802-11-wireless-security

[wifi-security]
key-mgmt=wpa-psk
proto=rsn;
pairwise=ccmp;
group=ccmp;
psk={psk}
psk-flags=0

[ipv4]
method=shared
address1={address}
never-default=true

[ipv6]
method=disabled
""".encode("utf-8")


def configure(args):
    require_root()
    address = validate_config(args.ssid, args.interface, args.address)
    password, generated = None, False
    if args.password is not None:
        password = validate_password(args.password)
    elif args.password_file is not None:
        password, generated = read_password(args.password_file)
    check_manager()
    check_wifi(args.interface)
    exists = profile_inventory()
    previous = owned_file()
    if exists and previous is None:
        raise HotspotError("The hotspot UUID exists outside the managed keyfile; refusing to replace it.")
    if CONNECTION_UUID in nmcli("--get-values", "UUID", "connection", "show", "--active").splitlines():
        raise HotspotError("Stop the hotspot before reconfiguring it: sudo python3 scripts/hotspot.py stop")
    psk = None
    if password is None:
        if previous is not None:
            psk = saved_psk(previous, args.ssid, exists)
        else:
            password, generated = read_password(None)
    atomic_write(render_keyfile(args.ssid, args.interface, address, password, psk=psk))
    try:
        nmcli("connection", "load", str(KEYFILE))
    except HotspotError as exc:
        if previous is None:
            KEYFILE.unlink()
            if profile_inventory():
                nmcli("connection", "delete", "uuid", CONNECTION_UUID)
        else:
            atomic_write(previous)
            try:
                nmcli("connection", "load", str(KEYFILE))
            except HotspotError:
                raise HotspotError("Configuration failed. Previous keyfile restored; NetworkManager could not reload it.") from exc
        raise HotspotError(f"Configuration failed; previous configuration restored. {exc}") from exc
    print(f"Configured SSID: {args.ssid} (not activated)")
    if generated:
        print(f"Wi-Fi password (save this now; shown once): {password}")
    elif psk is not None:
        print("Saved Wi-Fi password reused.")
    print(f"Browser URL after starting hotspot and app: http://{address.split('/')[0]}:{args.port}")


def profile_settings():
    fields = "connection.interface-name,802-11-wireless.ssid,ipv4.addresses,connection.autoconnect"
    values = nmcli("--escape", "no", "--get-values", fields, "connection", "show", "uuid", CONNECTION_UUID).splitlines()
    if len(values) != 4:
        raise HotspotError("Cannot read hotspot settings. Reconfigure the managed profile.")
    return values


def require_profile():
    if not profile_inventory() or owned_file() is None:
        raise HotspotError("Hotspot is not configured. Run sudo python3 scripts/hotspot.py configure first.")


def show_status(port):
    if not profile_inventory():
        print("Hotspot: not configured")
        return
    interface, ssid, address, autoconnect = profile_settings()
    active = CONNECTION_UUID in nmcli("--get-values", "UUID", "connection", "show", "--active").splitlines()
    print(f"SSID: {ssid}\nInterface: {interface}\nAddress: {address}\nAutoconnect: {autoconnect}")
    print(f"Active: {'yes' if active else 'no'}")
    print(f"Browser URL when hotspot and app are running: http://{address.split('/')[0]}:{port}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("configure", "start", "stop", "status"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--port", type=int, default=8080, help="Web app port for printed browser URL")
        if command == "configure":
            subparser.add_argument("--ssid", default="OAK-FFC-TEST")
            subparser.add_argument("--interface", default="wlan0")
            subparser.add_argument("--address", default="10.42.0.1/24")
            passwords = subparser.add_mutually_exclusive_group()
            passwords.add_argument("--password", help="Set a fixed 8–63 character ASCII WPA2 passphrase")
            passwords.add_argument("--password-file", type=Path, help="Local file containing an 8–63 character WPA2 passphrase")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        if args.command == "configure":
            configure(args)
        else:
            if args.command != "status":
                require_root()
            check_manager()
            if args.command == "start":
                require_profile()
                interface = profile_settings()[0]
                check_wifi(interface, activating=True)
                nmcli("connection", "up", "uuid", CONNECTION_UUID)
                nmcli("connection", "modify", "uuid", CONNECTION_UUID,
                      "connection.autoconnect", "yes", "connection.autoconnect-priority", "999")
            elif args.command == "stop":
                require_profile()
                nmcli("connection", "modify", "uuid", CONNECTION_UUID, "connection.autoconnect", "no")
                if CONNECTION_UUID in nmcli("--get-values", "UUID", "connection", "show", "--active").splitlines():
                    nmcli("connection", "down", "uuid", CONNECTION_UUID)
            show_status(args.port)
    except (HotspotError, OSError) as exc:
        print(f"Hotspot error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
