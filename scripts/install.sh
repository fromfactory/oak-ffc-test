#!/usr/bin/env bash
set -euo pipefail

project_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON_BIN:-python3}"

cat <<'INSTRUCTIONS'
OAK FFC TEST: project-local Python and frontend installation

If operating-system dependencies are missing, run these commands yourself:
  sudo apt-get update
  sudo apt-get install -y python3-venv python3-pip libusb-1.0-0 libglib2.0-0

For non-root OAK USB access, use this rule for users in the plugdev group:
  echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="03e7", MODE="0660", GROUP="plugdev"' | sudo tee /etc/udev/rules.d/80-oak-camera.rules
  sudo udevadm control --reload-rules
  sudo udevadm trigger --subsystem-match=usb
Then unplug and reconnect the OAK.

This installer does not run sudo or change operating-system settings.
Node.js 22.12+ (22.x) or 24+, with npm, is required to build the browser interface.
INSTRUCTIONS

if ! command -v "$python_bin" >/dev/null 2>&1; then
    printf 'Python interpreter not found: %s\n' "$python_bin" >&2
    exit 1
fi

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    printf 'Install Node.js 22.12+ (22.x) or 24+ with npm, then rerun this installer. See https://nodejs.org/en/download\n' >&2
    exit 1
fi

node --input-type=module - <<'JAVASCRIPT'
const [major, minor] = process.versions.node.split('.').map(Number);
if (!((major === 22 && minor >= 12) || major >= 24)) {
  console.error('Node.js 22.12+ (22.x) or 24+ is required to build the browser interface.');
  process.exit(1);
}
JAVASCRIPT

"$python_bin" - <<'PYTHON'
import struct
import sys

if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required. Set PYTHON_BIN to a supported interpreter.")
if struct.calcsize("P") != 8:
    raise SystemExit("Use a 64-bit Python installation supported by the pinned DepthAI package.")
PYTHON

if [[ ! -f "$project_dir/requirements.txt" ]]; then
    printf 'Missing dependency file: %s/requirements.txt\n' "$project_dir" >&2
    exit 1
fi

if [[ ! -x "$project_dir/.venv/bin/python" ]]; then
    "$python_bin" -m venv "$project_dir/.venv"
fi

"$project_dir/.venv/bin/python" - <<'PYTHON'
import struct
import sys

if sys.version_info < (3, 10) or struct.calcsize("P") != 8:
    raise SystemExit("The existing .venv must use 64-bit Python 3.10 or newer. Move it aside and rerun this installer.")
PYTHON

"$project_dir/.venv/bin/python" -m pip install --upgrade pip
"$project_dir/.venv/bin/python" -m pip install --only-binary=depthai -r "$project_dir/requirements.txt"

cd -- "$project_dir"
npm ci
npm run build

printf '\nInstallation complete. Run:\n  bash %q/scripts/run.sh\n' "$project_dir"
printf 'For demo mode, add --demo. See README.md for hardware and network setup.\n'
