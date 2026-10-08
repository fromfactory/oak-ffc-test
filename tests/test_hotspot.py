"""Exercise hotspot lifecycle with a fake NetworkManager; never change host networking."""

import configparser
import hashlib
from pathlib import Path
import subprocess

import pytest

from scripts import hotspot


PASSWORD = "Synthetic-Test-Passphrase"


@pytest.fixture
def network(tmp_path, monkeypatch):
    class Network:
        profiles = {}
        active = False
        managed = "yes"
        ap = "yes"
        state = "30 (disconnected)"
        radio = "enabled"
        fail_loads = 0
        fail_start = False
        partially_load = False
        running = "running"
        commands = []
        loaded = None
        decoded_ssid = None

        def run(self, argv, **kwargs):
            assert kwargs["capture_output"] and kwargs["text"]
            assert kwargs["env"]["LC_ALL"] == "C"
            assert kwargs["check"] is False
            assert PASSWORD not in argv
            assert "--show-secrets" not in argv
            assert argv[:5] == ["nmcli", "--wait", "30", "--colors", "no"]
            command = argv[5:]
            self.commands.append(command)
            output = ""
            error = ""
            code = 0
            if command == ["--get-values", "RUNNING", "general"]:
                output = self.running
            elif command[-3:-1] == ["device", "show"] and command[-1] in {"wlan0", "wlan1"}:
                output = f"wifi\n{self.managed}\n{self.ap}\n{self.state}"
            elif command == ["radio", "wifi"]:
                output = self.radio
            elif command == ["--terse", "--escape", "no", "--fields", "UUID,NAME", "connection", "show"]:
                output = "\n".join(f"{uuid}:{name}" for uuid, name in self.profiles.items())
            elif command == ["--get-values", "UUID", "connection", "show", "--active"]:
                output = hotspot.CONNECTION_UUID if self.active else ""
            elif command[:2] == ["connection", "load"]:
                if self.fail_loads:
                    self.fail_loads -= 1
                    if self.partially_load:
                        self.profiles[hotspot.CONNECTION_UUID] = hotspot.CONNECTION_ID
                    code, error = 1, "Synthetic keyfile load failure"
                else:
                    self.loaded = configparser.ConfigParser(interpolation=None)
                    self.loaded.read(command[2])
                    self.profiles[hotspot.CONNECTION_UUID] = hotspot.CONNECTION_ID
            elif command[:2] == ["connection", "delete"]:
                del self.profiles[hotspot.CONNECTION_UUID]
            elif command[:2] == ["connection", "up"]:
                if self.fail_start:
                    code, error = 4, "Synthetic activation failure"
                else:
                    self.active = True
            elif command[:2] == ["connection", "down"]:
                self.active = False
            elif command[:2] == ["connection", "modify"]:
                self.loaded["connection"]["autoconnect"] = command[5]
            elif command[:4] == ["--escape", "no", "--get-values",
                                   "connection.interface-name,802-11-wireless.ssid,ipv4.addresses,connection.autoconnect"]:
                ssid = self.loaded["wifi"]["ssid"]
                if self.decoded_ssid is not None:
                    ssid = self.decoded_ssid
                elif ssid.endswith(";") and all(byte.isdigit() for byte in ssid.split(";")[:-1]):
                    ssid = bytes(int(byte) for byte in ssid.split(";")[:-1]).decode("utf-8")
                output = (f"{self.loaded['connection']['interface-name']}\n{ssid}\n"
                          f"{self.loaded['ipv4']['address1']}\n{self.loaded['connection']['autoconnect']}")
            else:
                pytest.fail(f"Unexpected NetworkManager command: {command}")
            return subprocess.CompletedProcess(argv, code, output, error)

    fake = Network()
    fake.profiles, fake.commands = {}, []
    monkeypatch.setattr(hotspot, "KEYFILE", tmp_path / "oak-ffc-hotspot.nmconnection")
    monkeypatch.setattr(hotspot.os, "geteuid", lambda: 0)
    fake.owners = []
    monkeypatch.setattr(hotspot.os, "fchown", lambda fd, uid, gid: fake.owners.append((uid, gid)))
    monkeypatch.setattr(hotspot.shutil, "which", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr(hotspot.subprocess, "run", fake.run)
    return fake


def configure(network, tmp_path, *args):
    password_file = tmp_path / "local-password"
    password_file.write_text(PASSWORD + "\n", encoding="ascii")
    return hotspot.main(["configure", "--password-file", str(password_file), *args])


def test_configure_stages_secure_shared_profile_without_activation(network, tmp_path, capsys):
    assert configure(network, tmp_path) == 0
    profile = network.loaded
    assert profile["connection"]["id"] == hotspot.CONNECTION_ID
    assert profile["connection"]["uuid"] == hotspot.CONNECTION_UUID
    assert profile["connection"]["autoconnect"] == "false"
    assert profile["connection"]["autoconnect-priority"] == "999"
    assert profile["wifi"]["mode"] == "ap"
    assert profile["wifi"]["band"] == "bg"
    assert profile["wifi-security"]["key-mgmt"] == "wpa-psk"
    assert profile["wifi-security"]["proto"] == "rsn;"
    assert profile["wifi-security"]["pairwise"] == profile["wifi-security"]["group"] == "ccmp;"
    assert profile["wifi-security"]["psk"] == hashlib.pbkdf2_hmac(
        "sha1", PASSWORD.encode("ascii"), b"OAK-FFC-TEST", 4096, 32).hex()
    assert profile["ipv4"]["method"] == "shared"
    assert profile["ipv4"]["address1"] == "10.42.0.1/24"
    assert profile["ipv4"]["never-default"] == "true"
    assert profile["ipv6"]["method"] == "disabled"
    assert hotspot.KEYFILE.stat().st_mode & 0o777 == 0o600
    assert network.owners == [(0, 0)]
    assert PASSWORD not in hotspot.KEYFILE.read_text()
    assert PASSWORD not in capsys.readouterr().out
    assert not any(command[:2] == ["connection", "up"] for command in network.commands)
    assert not network.active


def test_generated_password_is_printed_once_after_success(network, monkeypatch, capsys):
    monkeypatch.setattr(hotspot.secrets, "token_urlsafe", lambda length: PASSWORD)
    assert hotspot.main(["configure"]) == 0
    output = capsys.readouterr().out
    assert output.count(PASSWORD) == 1
    assert "http://10.42.0.1:8080" in output
    assert all(PASSWORD not in command for command in network.commands)
    saved_psk = network.loaded["wifi-security"]["psk"]
    monkeypatch.setattr(hotspot.secrets, "token_urlsafe", lambda length: pytest.fail("Saved password must be reused"))
    assert hotspot.main(["configure"]) == 0
    output = capsys.readouterr()
    assert network.loaded["wifi-security"]["psk"] == saved_psk
    assert "saved" in output.out.lower() and "reuse" in output.out.lower()
    assert PASSWORD not in output.out + output.err
    assert saved_psk not in output.out + output.err


def test_explicit_password_is_derived_without_printing_or_passing_it_to_networkmanager(network, capsys):
    assert hotspot.main(["configure", "--password", PASSWORD]) == 0
    saved_psk = network.loaded["wifi-security"]["psk"]
    assert saved_psk == hashlib.pbkdf2_hmac("sha1", PASSWORD.encode("ascii"), b"OAK-FFC-TEST", 4096, 32).hex()
    assert PASSWORD not in hotspot.KEYFILE.read_text()
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err
    assert saved_psk not in output.out + output.err


@pytest.mark.parametrize("arguments,section,key,expected", [
    ([], "connection", "interface-name", "wlan0"),
    (["--address", "192.168.73.7/24"], "ipv4", "address1", "192.168.73.7/24"),
    (["--interface", "wlan1"], "connection", "interface-name", "wlan1"),
])
def test_reconfigure_reuses_saved_password_when_ssid_is_unchanged(
        network, tmp_path, monkeypatch, capsys, arguments, section, key, expected):
    assert configure(network, tmp_path) == 0
    saved_psk = network.loaded["wifi-security"]["psk"]
    capsys.readouterr()
    monkeypatch.setattr(hotspot.secrets, "token_urlsafe", lambda length: pytest.fail("Saved password must be reused"))
    assert hotspot.main(["configure", *arguments]) == 0
    assert network.loaded["wifi-security"]["psk"] == saved_psk
    assert network.loaded[section][key] == expected
    output = capsys.readouterr()
    assert "saved" in output.out.lower() and "reuse" in output.out.lower()
    assert PASSWORD not in output.out + output.err


@pytest.mark.parametrize("loaded", [True, False])
def test_reconfigure_reuses_password_after_networkmanager_rewrites_ssid_as_text(
        network, tmp_path, monkeypatch, capsys, loaded):
    assert configure(network, tmp_path) == 0
    saved_psk = network.loaded["wifi-security"]["psk"]
    network.loaded["wifi"]["ssid"] = "OAK-FFC-TEST"
    with hotspot.KEYFILE.open("w") as handle:
        network.loaded.write(handle)
    if not loaded:
        network.profiles.clear()
    capsys.readouterr()
    monkeypatch.setattr(hotspot.secrets, "token_urlsafe", lambda length: pytest.fail("Saved password must be reused"))
    assert hotspot.main(["configure"]) == 0
    assert network.loaded["wifi-security"]["psk"] == saved_psk
    output = capsys.readouterr()
    assert "saved" in output.out.lower() and "reuse" in output.out.lower()
    assert PASSWORD not in output.out + output.err


@pytest.mark.parametrize("ssid,result", [("OAK", 0), ("79;65;75;", 1)])
def test_unloaded_byte_ssid_is_compared_as_decoded_bytes(network, tmp_path, monkeypatch, capsys, ssid, result):
    assert configure(network, tmp_path, "--ssid", "OAK") == 0
    original = hotspot.KEYFILE.read_bytes()
    saved_psk = network.loaded["wifi-security"]["psk"]
    owners = network.owners[:]
    network.profiles.clear()
    network.commands.clear()
    capsys.readouterr()
    monkeypatch.setattr(hotspot.secrets, "token_urlsafe", lambda length: pytest.fail("Saved password must not be regenerated"))
    assert hotspot.main(["configure", "--ssid", ssid]) == result
    if result == 0:
        assert network.loaded["wifi-security"]["psk"] == saved_psk
    else:
        assert hotspot.KEYFILE.read_bytes() == original
        assert network.owners == owners
        assert not any(command[0] == "connection" for command in network.commands)
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err


def test_loaded_unicode_and_escaped_ssid_reuses_saved_password(network, tmp_path, monkeypatch, capsys):
    ssid = "測定;\\=# WiFi"
    assert configure(network, tmp_path, "--ssid", ssid) == 0
    saved_psk = network.loaded["wifi-security"]["psk"]
    network.loaded["wifi"]["ssid"] = ssid.replace("\\", "\\\\")
    network.decoded_ssid = ssid
    with hotspot.KEYFILE.open("w") as handle:
        network.loaded.write(handle)
    capsys.readouterr()
    monkeypatch.setattr(hotspot.secrets, "token_urlsafe", lambda length: pytest.fail("Saved password must be reused"))
    assert hotspot.main(["configure", "--ssid", ssid]) == 0
    assert network.loaded["wifi-security"]["psk"] == saved_psk
    output = capsys.readouterr()
    assert "saved" in output.out.lower() and "reuse" in output.out.lower()
    assert PASSWORD not in output.out + output.err


def test_changing_ssid_requires_explicit_password_and_preserves_saved_profile(network, tmp_path, monkeypatch, capsys):
    assert configure(network, tmp_path) == 0
    original = hotspot.KEYFILE.read_bytes()
    owners = network.owners[:]
    network.commands.clear()
    capsys.readouterr()
    monkeypatch.setattr(hotspot.secrets, "token_urlsafe", lambda length: pytest.fail("SSID change must not generate a password"))
    assert hotspot.main(["configure", "--ssid", "Replacement"]) == 1
    assert hotspot.KEYFILE.read_bytes() == original
    assert network.owners == owners
    assert not any(command[:2] in [["connection", action] for action in ("load", "delete", "up", "down", "modify")]
                   for command in network.commands)
    output = capsys.readouterr()
    assert "--password" in output.err
    assert PASSWORD not in output.out + output.err


@pytest.mark.parametrize("password_option", ["--password", "--password-file"])
def test_explicit_password_allows_ssid_change_with_correct_derived_key(network, tmp_path, password_option):
    assert configure(network, tmp_path) == 0
    previous_psk = network.loaded["wifi-security"]["psk"]
    value = PASSWORD if password_option == "--password" else str(tmp_path / "local-password")
    assert hotspot.main(["configure", "--ssid", "Replacement", password_option, value]) == 0
    expected_psk = hashlib.pbkdf2_hmac("sha1", PASSWORD.encode("ascii"), b"Replacement", 4096, 32).hex()
    assert network.loaded["wifi-security"]["psk"] == expected_psk
    assert expected_psk != previous_psk


def test_password_options_are_mutually_exclusive_before_network_or_file_access(network, tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        hotspot.main(["configure", "--password", PASSWORD, "--password-file", str(tmp_path / "missing-password")])
    assert error.value.code == 2
    assert network.commands == []
    assert not hotspot.KEYFILE.exists()
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err


@pytest.mark.parametrize("saved_psk", [None, "", "a" * 63, "z" * 64])
def test_reconfigure_refuses_missing_or_invalid_saved_key_without_mutations(
        network, tmp_path, monkeypatch, capsys, saved_psk):
    assert configure(network, tmp_path) == 0
    profile = configparser.ConfigParser(interpolation=None)
    profile.read(hotspot.KEYFILE)
    if saved_psk is None:
        del profile["wifi-security"]["psk"]
    else:
        profile["wifi-security"]["psk"] = saved_psk
    with hotspot.KEYFILE.open("w") as handle:
        profile.write(handle)
    original = hotspot.KEYFILE.read_bytes()
    owners = network.owners[:]
    network.commands.clear()
    capsys.readouterr()
    monkeypatch.setattr(hotspot.secrets, "token_urlsafe", lambda length: pytest.fail("Invalid saved key must not generate a password"))
    assert hotspot.main(["configure"]) == 1
    assert hotspot.KEYFILE.read_bytes() == original
    assert network.owners == owners
    assert not any(command[:2] in [["connection", action] for action in ("load", "delete", "up", "down", "modify")]
                   for command in network.commands)
    assert PASSWORD not in capsys.readouterr().err


def test_unicode_and_keyfile_metacharacters_do_not_change_profile_structure(network, tmp_path):
    ssid = "測定;\\=# WiFi"
    assert configure(network, tmp_path, "--ssid", ssid, "--address", "192.168.73.7/24") == 0
    assert bytes(int(byte) for byte in network.loaded["wifi"]["ssid"].split(";") if byte).decode() == ssid
    assert network.loaded["ipv4"]["address1"] == "192.168.73.7/24"


@pytest.mark.parametrize("arguments", [
    ["--ssid", ""], ["--ssid", "測" * 11], ["--ssid", "SSID\n[ipv4]"],
    ["--ssid", "SSID\u2028Next"], ["--interface", "wlan0\n"], ["--interface=--bad"], ["--interface", "x" * 16],
    ["--address", "8.8.8.8/24"], ["--address", "127.0.0.1/24"],
    ["--address", "10.42.0.0/24"], ["--address", "10.42.0.255/24"],
    ["--address", "10.42.0.1/16"], ["--address", "::1/24"],
])
def test_invalid_configuration_never_mutates_network_or_files(network, tmp_path, arguments):
    assert configure(network, tmp_path, *arguments) == 1
    assert network.commands == []
    assert not hotspot.KEYFILE.exists()


@pytest.mark.parametrize("password_option", ["--password", "--password-file"])
@pytest.mark.parametrize("password", ["short", "x" * 64, "x" * 8 + "\nnew-line", "nonascii-パスワード"])
def test_invalid_password_is_not_echoed_or_used(network, tmp_path, capsys, password, password_option):
    value = password
    if password_option == "--password-file":
        password_file = tmp_path / "invalid-password"
        password_file.write_text(password)
        value = str(password_file)
    assert hotspot.main(["configure", password_option, value]) == 1
    assert password not in capsys.readouterr().err
    assert network.commands == []
    assert not hotspot.KEYFILE.exists()


def test_mutating_commands_require_root(network, monkeypatch):
    monkeypatch.setattr(hotspot.os, "geteuid", lambda: 1000)
    for command in ("configure", "start", "stop"):
        assert hotspot.main([command]) == 1
    assert network.commands == []


@pytest.mark.parametrize("uuid,name", [("unrelated-uuid", hotspot.CONNECTION_ID),
                                      (hotspot.CONNECTION_UUID, "Unrelated profile")])
def test_profile_collisions_are_refused(network, tmp_path, uuid, name):
    network.profiles[uuid] = name
    assert configure(network, tmp_path) == 1
    assert not hotspot.KEYFILE.exists()
    assert not any(command[:2] in [["connection", "load"], ["connection", "modify"]]
                   for command in network.commands)


def test_existing_unrelated_keyfile_is_preserved(network, tmp_path):
    content = "[connection]\nid=Unrelated\nuuid=not-ours\n"
    hotspot.KEYFILE.write_text(content)
    assert configure(network, tmp_path) == 1
    assert hotspot.KEYFILE.read_text() == content


def test_keyfile_symlink_is_refused_without_modifying_target(network, tmp_path):
    target = tmp_path / "unrelated-file"
    target.write_text("Unrelated data")
    hotspot.KEYFILE.symlink_to(target)
    assert configure(network, tmp_path) == 1
    assert hotspot.KEYFILE.is_symlink()
    assert target.read_text() == "Unrelated data"


def test_owned_uuid_outside_managed_keyfile_is_refused(network, tmp_path):
    network.profiles[hotspot.CONNECTION_UUID] = hotspot.CONNECTION_ID
    assert configure(network, tmp_path) == 1
    assert not hotspot.KEYFILE.exists()


def test_load_failure_removes_new_file_and_does_not_print_generated_secret(network, monkeypatch, capsys):
    network.fail_loads = 1
    monkeypatch.setattr(hotspot.secrets, "token_urlsafe", lambda length: PASSWORD)
    assert hotspot.main(["configure"]) == 1
    assert not hotspot.KEYFILE.exists()
    assert not network.profiles
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err
    assert not any(command[:2] == ["connection", "up"] for command in network.commands)


def test_load_failure_restores_previous_profile_atomically(network, tmp_path):
    assert configure(network, tmp_path) == 0
    original = hotspot.KEYFILE.read_bytes()
    network.fail_loads = 1
    assert configure(network, tmp_path, "--ssid", "Replacement") == 1
    assert hotspot.KEYFILE.read_bytes() == original
    assert network.loaded["wifi"]["ssid"] == "".join(f"{b};" for b in b"OAK-FFC-TEST")
    assert hotspot.KEYFILE.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".oak-ffc-hotspot-*")) == []


def test_partial_load_failure_deletes_only_new_owned_profile(network, tmp_path):
    network.profiles["unrelated-uuid"] = "Home Wi-Fi"
    network.fail_loads, network.partially_load = 1, True
    assert configure(network, tmp_path) == 1
    assert network.profiles == {"unrelated-uuid": "Home Wi-Fi"}
    assert not hotspot.KEYFILE.exists()
    assert ["connection", "delete", "uuid", hotspot.CONNECTION_UUID] in network.commands


def test_atomic_replace_failure_preserves_old_file_and_removes_temporary_secret(network, tmp_path, monkeypatch):
    assert configure(network, tmp_path) == 0
    original = hotspot.KEYFILE.read_bytes()

    def fail_replace(*args):
        raise OSError("Synthetic disk write failure")

    monkeypatch.setattr(hotspot.os, "replace", fail_replace)
    assert configure(network, tmp_path, "--ssid", "Replacement") == 1
    assert hotspot.KEYFILE.read_bytes() == original
    assert list(tmp_path.glob(".oak-ffc-hotspot-*")) == []


def test_reconfigure_refuses_active_hotspot_without_changing_it(network, tmp_path):
    assert configure(network, tmp_path) == 0
    original = hotspot.KEYFILE.read_bytes()
    network.active = True
    assert configure(network, tmp_path, "--ssid", "Replacement") == 1
    assert hotspot.KEYFILE.read_bytes() == original
    assert network.active


def test_start_activates_before_enabling_autoconnect_and_stop_disables_it(network, tmp_path, capsys):
    assert configure(network, tmp_path) == 0
    network.commands.clear()
    assert hotspot.main(["start", "--port", "9090"]) == 0
    assert network.active
    assert network.loaded["connection"]["autoconnect"] == "yes"
    mutations = [c for c in network.commands if c[0] == "connection"]
    assert [c[1] for c in mutations] == ["up", "modify"]
    assert "http://10.42.0.1:9090" in capsys.readouterr().out
    network.commands.clear()
    assert hotspot.main(["stop"]) == 0
    assert not network.active
    assert network.loaded["connection"]["autoconnect"] == "no"
    mutations = [c for c in network.commands if c[0] == "connection"]
    assert [c[1] for c in mutations] == ["modify", "down"]
    # Connection down leaves NetworkManager free to reconnect a saved Wi-Fi profile.
    assert not any(c[:2] == ["device", "disconnect"] for c in network.commands)


def test_stop_is_safe_when_already_inactive(network, tmp_path):
    assert configure(network, tmp_path) == 0
    network.commands.clear()
    assert hotspot.main(["stop"]) == 0
    assert not any(c[:2] == ["connection", "down"] for c in network.commands)
    assert network.loaded["connection"]["autoconnect"] == "no"


def test_failed_start_does_not_enable_autoconnect(network, tmp_path):
    assert configure(network, tmp_path) == 0
    network.fail_start = True
    assert hotspot.main(["start"]) == 1
    assert network.loaded["connection"]["autoconnect"] == "false"


def test_configuration_can_be_staged_with_disabled_radio_but_start_requires_available_wifi(network, tmp_path, capsys):
    network.state, network.radio = "20 (unavailable)", "disabled"
    assert configure(network, tmp_path) == 0
    assert hotspot.main(["start"]) == 1
    assert "rfkill" in capsys.readouterr().err
    assert not any(c[:2] == ["connection", "up"] for c in network.commands)
    assert not any(c[:2] == ["radio", "wifi"] and len(c) == 3 for c in network.commands)


@pytest.mark.parametrize("field,value", [("ap", "no"), ("managed", "no")])
def test_unsupported_or_unmanaged_wifi_is_not_changed(network, tmp_path, field, value):
    setattr(network, field, value)
    assert configure(network, tmp_path) == 1
    assert not hotspot.KEYFILE.exists()


def test_missing_dhcp_prerequisite_is_reported_before_writing(network, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hotspot.shutil, "which", lambda name: None)
    is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda path: False if path == Path("/usr/sbin/dnsmasq") else is_file(path))
    assert configure(network, tmp_path) == 1
    assert "sudo apt install dnsmasq-base" in capsys.readouterr().err
    assert not hotspot.KEYFILE.exists()


def test_stopped_networkmanager_is_reported_before_writing(network, tmp_path, capsys):
    network.running = "not running"
    assert configure(network, tmp_path) == 1
    assert "NetworkManager is not running" in capsys.readouterr().err
    assert not hotspot.KEYFILE.exists()


def test_status_does_not_require_root_or_read_private_keyfile(network, tmp_path, monkeypatch, capsys):
    assert configure(network, tmp_path) == 0
    capsys.readouterr()
    monkeypatch.setattr(hotspot.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(hotspot, "owned_file", lambda: pytest.fail("Status must not read credentials"))
    assert hotspot.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "SSID: OAK-FFC-TEST" in output and "Active: no" in output
    assert PASSWORD not in output
    assert "http://10.42.0.1:8080" in output
