"""Local camera workbench for the USB OAK-FFC 4P platform."""

import os

# DepthAI 2.30 caches these settings: disable uploads before the SDK is used.
# Any nonempty analytics value enables uploads; local crash files may still be saved.
os.environ["DEPTHAI_DISABLE_CRASHDUMP_COLLECTION"] = "1"
os.environ["DEPTHAI_ENABLE_ANALYTICS_COLLECTION"] = ""

__version__ = "1.0.0"
