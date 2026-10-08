"""Verify unattended setup with fake systemd, NetworkManager, accounts, and HTTP."""

from io import BytesIO
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import install_service as service


@pytest.fixture
def machine(tmp_path, monkeypatch):
    project = tmp_path / "project with spaces"
    python = project / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("fake executable")
    python.chmod(0o755)
    (project / "captures").mkdir()
    runtime = tmp_path / "systemd-runtime"
    runtime.mkdir()
    unit = tmp_path / service.UNIT_NAME
    state = SimpleNamespace(project=project, unit=unit, active=False, enabled=False,
                            commands=[], network=[], configured=[], owners=[], fail=None,
                            dependency_error=None, busy=False, clock=0, http_ready=True,
                            saved=True, loaded=True, ssid="Personal Pi", interface="wlan1",
                            address="192.168.56.1/24", wifi_active=True)
    monkeypatch.setattr(service, "PROJECT", project)
    monkeypatch.setattr(service, "UNIT_PATH", unit)
    monkeypatch.setattr(service, "SYSTEMD_RUNTIME", runtime)
    monkeypatch.setattr(service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(service.os, "getgrouplist", lambda name, gid: [gid, 44, 46])
    monkeypatch.setattr(service.os, "fchown", lambda fd, uid, gid: state.owners.append((uid, gid)))
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(service.pwd, "getpwnam", lambda name: SimpleNamespace(
        pw_name=name, pw_uid=1000, pw_gid=1000))
    monkeypatch.setattr(service.grp, "getgrgid", lambda gid: SimpleNamespace(gr_name="pi"))

    def run(argv, **kwargs):
        if argv[0] == str(python):
            assert kwargs["user"] == kwargs["group"] == 1000
            assert kwargs["extra_groups"] == [1000, 44, 46]
            assert kwargs["cwd"] == project
            assert kwargs["env"]["DEPTHAI_DISABLE_CRASHDUMP_COLLECTION"] == "1"
            assert kwargs["env"]["DEPTHAI_ENABLE_ANALYTICS_COLLECTION"] == ""
            assert argv[-1] == str(project / "captures") or "photos" in argv[-1]
            return subprocess.CompletedProcess(argv, bool(state.dependency_error), "", state.dependency_error or "")
        assert argv[0] == "systemctl"
        assert kwargs["check"] is False
        assert kwargs["env"]["LC_ALL"] == "C"
        command = argv[1:]
        state.commands.append(command)
        if command[0] == "is-active":
            return subprocess.CompletedProcess(argv, 0 if state.active else 3, "", "")
        if command[0] == "is-enabled":
            return subprocess.CompletedProcess(argv, 0 if state.enabled else 1, "", "")
        if command == state.fail:
            state.fail = None
            return subprocess.CompletedProcess(argv, 1, "", "Synthetic control failure")
        if command[-1] == service.UNIT_NAME:
            if command[0] == "enable":
                state.enabled = True
            elif command[0] == "disable":
                state.enabled = False
            elif command[0] == "restart":
                state.active = True
            elif command[0] == "stop":
                state.active = False
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(service.subprocess, "run", run)

    class Listener:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, address):
            assert address[0] == "0.0.0.0"
            if state.busy:
                raise OSError("Synthetic occupied port")

    monkeypatch.setattr(service.socket, "socket", lambda *args: Listener())
    monkeypatch.setattr(service.time, "monotonic", lambda: state.clock)
    monkeypatch.setattr(service.time, "sleep", lambda seconds: setattr(state, "clock", state.clock + seconds))

    class Opener:
        def open(self, url, timeout):
            assert url.startswith("http://127.0.0.1:") and url.endswith("/api/status")
            assert 0 < timeout <= 2
            if not state.http_ready:
                raise OSError("Synthetic connection refused")
            return BytesIO(b'{"cameras": [], "running": false}')

    monkeypatch.setattr(service.urllib.request, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(service.hotspot, "check_manager", lambda: state.network.append(("check-manager",)))
    monkeypatch.setattr(service.hotspot, "check_wifi", lambda interface, **kwargs:
                        state.network.append(("check-wifi", interface, kwargs)))
    monkeypatch.setattr(service.hotspot, "profile_inventory", lambda: state.loaded)
    monkeypatch.setattr(service.hotspot, "owned_file", lambda:
                        service.hotspot.render_keyfile(state.ssid, state.interface, state.address,
                                                      "Synthetic Password") if state.saved else None)
    monkeypatch.setattr(service.hotspot, "profile_settings", lambda:
                        [state.interface, state.ssid, state.address, "no"])

    def configure(args):
        state.configured.append(args)
        state.loaded = state.saved = True
        state.interface, state.ssid, state.address = args.interface, args.ssid, args.address

    monkeypatch.setattr(service.hotspot, "configure", configure)

    def nmcli(*args):
        assert "Synthetic Password" not in args
        state.network.append(args)
        if args == ("--get-values", "UUID", "connection", "show", "--active"):
            return service.hotspot.CONNECTION_UUID if state.wifi_active else ""
        if args[:2] == ("connection", "down"):
            state.wifi_active = False
        elif args[:2] == ("connection", "up"):
            state.wifi_active = True
        return ""

    monkeypatch.setattr(service.hotspot, "nmcli", nmcli)
    monkeypatch.setenv("SUDO_USER", "pi")
    return state


def test_install_preserves_hotspot_and_enables_web_app(machine, capsys):
    assert service.main([]) == 0
    text = machine.unit.read_text()
    assert 'User=pi\n' in text and 'Group=pi\n' in text
    assert '"--host" "0.0.0.0"' in text and '"--port" "8080"' in text
    assert str(machine.project / "captures") in text
    assert "Type=exec" in text and "KillSignal=SIGINT" in text
    assert "TimeoutStopSec=45" in text and "StartLimitIntervalSec=0" in text
    assert "Restart=on-failure" in text and "RestartSec=5" in text
    assert "After=NetworkManager.service" in text and "WantedBy=multi-user.target" in text
    assert "nmcli" not in text and "--demo" not in text
    assert not machine.configured
    assert machine.unit.stat().st_mode & 0o777 == 0o644
    assert machine.owners == [(0, 0)]
    assert ["enable", "NetworkManager.service"] in machine.commands
    controls = [command for command in machine.commands if command[0] not in {"is-active", "is-enabled"}]
    assert controls[-3:] == [["daemon-reload"], ["enable", service.UNIT_NAME], ["restart", service.UNIT_NAME]]
    assert ("radio", "wifi", "on") in machine.network
    assert ("connection", "modify", "uuid", service.hotspot.CONNECTION_UUID,
            "connection.autoconnect", "yes", "connection.autoconnect-priority", "999",
            "connection.autoconnect-retries", "0") in machine.network
    assert ("connection", "up", "uuid", service.hotspot.CONNECTION_UUID) in machine.network
    assert machine.active and machine.enabled
    output = capsys.readouterr().out
    assert "Personal Pi" in output and "http://192.168.56.1:8080" in output


def test_first_install_configures_hotspot_once(machine):
    machine.saved = machine.loaded = machine.wifi_active = False
    assert service.main([]) == 0
    settings = machine.configured[0]
    assert (settings.ssid, settings.interface, settings.address) == ("OAK-FFC-TEST", "wlan0", "10.42.0.1/24")
    assert settings.password is None
    assert service.main([]) == 0
    assert len(machine.configured) == 1


def test_password_change_preserves_network_settings_and_stops_owned_ap(machine):
    assert service.main(["--password", "Synthetic Password"]) == 0
    settings = machine.configured[0]
    assert (settings.ssid, settings.interface, settings.address) == ("Personal Pi", "wlan1", "192.168.56.1/24")
    assert settings.password == "Synthetic Password"
    assert ("connection", "down", "uuid", service.hotspot.CONNECTION_UUID) in machine.network


def test_saved_unloaded_profile_is_loaded_without_reconfiguration(machine):
    machine.loaded = False
    assert service.main([]) == 0
    assert ("connection", "load", str(service.hotspot.KEYFILE)) in machine.network
    assert not machine.configured


def test_custom_port_capture_and_explicit_demo(machine):
    assert service.main(["--port", "9090", "--capture-dir", "photos", "--demo"]) == 0
    text = machine.unit.read_text()
    assert '"--port" "9090"' in text and '"--demo"' in text
    assert str(machine.project / "photos") in text


@pytest.mark.parametrize("port", [0, 80, 1023, 65536])
def test_invalid_unprivileged_port_fails_before_mutation(machine, port):
    assert service.main(["--port", str(port)]) == 1
    assert not machine.commands and not machine.network and not machine.unit.exists()


@pytest.mark.parametrize("user", ["root", ""])
def test_root_or_missing_ordinary_user_fails_before_mutation(machine, user):
    assert service.main(["--user", user]) == 1
    assert not machine.network and not machine.commands


def test_nonroot_requires_sudo(machine, monkeypatch):
    monkeypatch.setattr(service.os, "geteuid", lambda: 1000)
    assert service.main([]) == 1
    assert not machine.network and not machine.commands


def test_root_login_requires_explicit_user(machine, monkeypatch):
    monkeypatch.delenv("SUDO_USER")
    assert service.main([]) == 1
    assert service.main(["--user", "pi"]) == 0


def test_unknown_account_fails_before_mutation(machine, monkeypatch):
    def missing(name):
        raise KeyError(name)
    monkeypatch.setattr(service.pwd, "getpwnam", missing)
    assert service.main(["--user", "unknown"]) == 1
    assert not machine.network and not machine.commands


@pytest.mark.parametrize("conflict", ["unmanaged", "symlink"])
def test_conflicting_unit_is_not_overwritten_or_wifi_changed(machine, conflict):
    if conflict == "symlink":
        machine.unit.symlink_to(machine.project / "missing-unit")
    else:
        machine.unit.write_text("[Service]\nExecStart=/bin/true\n")
    assert service.main([]) == 1
    assert not machine.network and not machine.commands
    assert machine.unit.is_symlink() if conflict == "symlink" else machine.unit.read_text().startswith("[Service]")


def test_dependency_failure_does_not_change_network(machine, capsys):
    machine.dependency_error = "No module named depthai"
    assert service.main([]) == 1
    assert not machine.network and not machine.unit.exists()
    assert "depthai" in capsys.readouterr().err


def test_occupied_port_requests_manual_app_stop(machine, capsys):
    machine.busy = True
    assert service.main([]) == 1
    assert not machine.network and not machine.unit.exists()
    assert "Ctrl+C" in capsys.readouterr().err


def test_existing_active_owned_service_can_be_updated(machine):
    machine.unit.write_text(service.render_unit(machine.project, "pi", "pi", 8080, machine.project / "captures"))
    machine.active = machine.enabled = machine.busy = True
    assert service.main(["--demo"]) == 0
    assert '"--demo"' in machine.unit.read_text()
    assert ["restart", service.UNIT_NAME] in machine.commands


def test_service_control_failure_restores_previous_unit_and_state(machine, capsys):
    original = service.render_unit(machine.project, "pi", "pi", 8080, machine.project / "captures").encode()
    machine.unit.write_bytes(original)
    machine.active = machine.enabled = True
    machine.fail = ["restart", service.UNIT_NAME]
    assert service.main(["--demo"]) == 1
    assert machine.unit.read_bytes() == original
    assert machine.active and machine.enabled
    assert "restored" in capsys.readouterr().err


def test_readiness_failure_rolls_back_first_install_and_does_not_claim_success(machine, capsys):
    machine.http_ready = False
    assert service.main([]) == 1
    assert not machine.unit.exists() and not machine.active and not machine.enabled
    assert machine.clock == 30
    output = capsys.readouterr()
    assert "Installed and running" not in output.out
    assert "journalctl" in output.err and "Hotspot settings remain saved" in output.err


@pytest.mark.parametrize("value", ["bad\npath", "bad\rpath", "bad\x00path", "bad\x1fpath", "bad\x7fpath"])
def test_control_characters_cannot_inject_unit_directives(machine, value):
    assert service.main(["--capture-dir", value]) == 1
    assert not machine.network and not machine.commands


def test_systemd_quotes_path_specifiers_and_exec_environment_without_shell():
    project = Path('/srv/Pi camera 100%$USER')
    captures = project / 'saved "files"\\100%$USER'
    text = service.render_unit(project, "pi", "video", 8080, captures)
    assert 'WorkingDirectory=/srv/Pi camera 100%%$USER/.\n' in text
    assert '"/srv/Pi camera 100%%$USER/.venv/bin/python"' in text
    assert '"/srv/Pi camera 100%%$$USER/saved \\"files\\"\\\\100%%$$USER"' in text
    assert '/bin/sh' not in text


def test_working_directory_keeps_trailing_spaces():
    text = service.render_unit(Path('/srv/camera space '), "pi", "video", 8080, '/tmp/photos')
    assert 'WorkingDirectory=/srv/camera space /.\n' in text


@pytest.mark.parametrize("project", [Path('/srv/camera"'), Path('/srv/camera\\')])
def test_unsupported_executable_characters_fail_before_mutation(machine, monkeypatch, project):
    monkeypatch.setattr(service, "PROJECT", project)
    assert service.main([]) == 1
    assert not machine.network and not machine.commands


def test_rendered_unit_passes_actual_systemd_parser_when_available(tmp_path):
    analyzer = shutil.which("systemd-analyze")
    if not analyzer:
        pytest.skip("systemd-analyze is not installed")
    project = tmp_path / 'Pi camera 100%$USER'
    executable = project / ".venv/bin/python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)
    account = service.pwd.getpwuid(os.getuid())
    group = service.grp.getgrgid(account.pw_gid).gr_name
    unit = tmp_path / service.UNIT_NAME
    unit.write_text(service.render_unit(project, account.pw_name, group, 8080,
                                       project / 'captures "files"\\100%$USER'))
    result = subprocess.run([analyzer, "verify", str(unit)], capture_output=True, text=True,
                            timeout=30, check=False)
    if "SO_PASSCRED failed: Operation not permitted" in result.stderr:
        pytest.skip("execution sandbox blocks systemd parser manager initialization")
    assert result.returncode == 0, result.stderr


def test_invalid_password_fails_before_network_mutation(machine):
    assert service.main(["--password", "short"]) == 1
    assert not machine.network and not machine.unit.exists()


def test_mutually_exclusive_password_options(machine):
    with pytest.raises(SystemExit):
        service.main(["--password", "Synthetic Password", "--password-file", "passphrase"])
    assert not machine.network and not machine.commands
