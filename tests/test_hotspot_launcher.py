"""Run the hotspot launcher with fake commands; never change networking."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def launcher(tmp_path):
    project = tmp_path / "checkout with spaces"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "scripts"
    for name in ("run_hotspot.sh", "run.sh"):
        shutil.copy2(source / name, scripts / name)

    commands = tmp_path / "commands"
    commands.mkdir()
    # A controlled PATH also lets the missing-nmcli test work on hosts with nmcli.
    for command in ("bash", "dirname"):
        (commands / command).symlink_to(shutil.which(command))

    nmcli = commands / "nmcli"
    nmcli.write_text(f"#!{sys.executable}\n" + """
import json
import os
from pathlib import Path
import sys

Path(os.environ["NMCLI_CALL"]).write_text(json.dumps(sys.argv[1:]))
sys.stdout.write(os.environ["HOTSPOT_ADDRESS"] + "\\n")
raise SystemExit(int(os.environ.get("NMCLI_EXIT", "0")))
""")
    nmcli.chmod(0o755)

    python = project / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f"#!{sys.executable}\n" + """
import json
import os
from pathlib import Path
import sys

Path(os.environ["APP_CALL"]).write_text(json.dumps({
    "args": sys.argv[1:], "cwd": os.getcwd(),
}))
""")
    python.chmod(0o755)
    env = {**os.environ, "PATH": str(commands), "HOTSPOT_ADDRESS": "10.42.0.1/24",
           "NMCLI_CALL": str(tmp_path / "nmcli.json"),
           "APP_CALL": str(tmp_path / "app.json")}

    def run(*arguments, address=None, nmcli_exit=None):
        invocation_env = env.copy()
        if address is not None:
            invocation_env["HOTSPOT_ADDRESS"] = address
        if nmcli_exit is not None:
            invocation_env["NMCLI_EXIT"] = str(nmcli_exit)
        return subprocess.run([str(commands / "bash"), str(scripts / "run_hotspot.sh"),
                               *arguments], env=invocation_env, cwd=tmp_path,
                              capture_output=True, text=True, timeout=10)

    return SimpleNamespace(run=run, project=project, nmcli=nmcli,
                           nmcli_call=Path(env["NMCLI_CALL"]),
                           app_call=Path(env["APP_CALL"]))


@pytest.mark.parametrize(("address", "options", "url"), [
    ("10.42.0.1/24", [], "http://10.42.0.1:8080"),
    ("10.55.66.1/24", [], "http://10.55.66.1:8080"),
    ("10.55.66.1/24", ["--port", "9090"], "http://10.55.66.1:9090"),
    ("10.55.66.1/24", ["--port=9091"], "http://10.55.66.1:9091"),
])
def test_launcher_uses_configured_address_and_forwards_port(launcher, address, options, url):
    result = launcher.run(*options, address=address)
    assert result.returncode == 0, result.stderr
    assert f"open {url}\n" in result.stdout
    assert json.loads(launcher.nmcli_call.read_text()) == [
        "--get-values", "ipv4.addresses", "connection", "show", "id", "oak-ffc-hotspot",
    ]
    call = json.loads(launcher.app_call.read_text())
    assert call == {"args": ["-m", "oak_camera", "--host", "0.0.0.0", *options],
                    "cwd": str(launcher.project)}


def test_launcher_preserves_demo_and_capture_path_with_spaces(launcher, tmp_path):
    capture_dir = str(tmp_path / "captures with spaces")
    result = launcher.run("--demo", "--capture-dir", capture_dir)
    assert result.returncode == 0, result.stderr
    assert "open http://10.42.0.1:8080\n" in result.stdout
    assert json.loads(launcher.app_call.read_text())["args"] == [
        "-m", "oak_camera", "--host", "0.0.0.0", "--demo", "--capture-dir", capture_dir,
    ]


@pytest.mark.parametrize("address", ["", "10.42.0.1", "10.42.0.1/16",
                                    "10.42.0.1/24,10.42.0.2/24", "999.42.0.1/24"])
def test_launcher_rejects_bad_profile_address_without_starting_app(launcher, address):
    result = launcher.run(address=address)
    assert result.returncode != 0
    assert "Unexpected hotspot address" in result.stderr
    assert not launcher.app_call.exists()


def test_launcher_requires_configured_profile_without_starting_app(launcher):
    result = launcher.run(nmcli_exit=10)
    assert result.returncode != 0
    assert "Configure the hotspot first" in result.stderr
    assert not launcher.app_call.exists()


def test_launcher_requires_networkmanager_without_starting_app(launcher):
    launcher.nmcli.unlink()
    result = launcher.run()
    assert result.returncode != 0
    assert "NetworkManager is required" in result.stderr
    assert not launcher.app_call.exists()
