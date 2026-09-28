"""Check SDK privacy defaults in fresh processes without loading hardware code."""

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("analytics_value", ["1", "0"])
def test_package_import_disables_sdk_uploads_without_loading_sdk(analytics_value):
    env = os.environ.copy()
    env["DEPTHAI_DISABLE_CRASHDUMP_COLLECTION"] = ""
    # DepthAI 2.30 treats even "0" as enabled, because it checks nonemptiness.
    env["DEPTHAI_ENABLE_ANALYTICS_COLLECTION"] = analytics_value
    script = textwrap.dedent(
        """
        import json
        import os
        import sys

        class BlockDepthAI:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "depthai" or fullname.startswith("depthai."):
                    raise AssertionError("Package initialization tried to import DepthAI")
                return None

        def block_usb_access(event, args):
            if event == "open" and args and isinstance(args[0], (str, bytes)):
                path = os.fsdecode(args[0])
                if path == "/dev/bus/usb" or path.startswith("/dev/bus/usb/"):
                    raise AssertionError("Package initialization tried to open USB")

        sys.meta_path.insert(0, BlockDepthAI())
        sys.addaudithook(block_usb_access)
        import oak_camera

        print(json.dumps({
            "crash_uploads_disabled": os.environ["DEPTHAI_DISABLE_CRASHDUMP_COLLECTION"],
            "pipeline_analytics": os.environ["DEPTHAI_ENABLE_ANALYTICS_COLLECTION"],
            "sdk_imported": "depthai" in sys.modules,
            "version": oak_camera.__version__,
        }))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == {
        "crash_uploads_disabled": "1",
        "pipeline_analytics": "",
        "sdk_imported": False,
        "version": "1.0.0",
    }
