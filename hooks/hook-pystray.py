"""Collect only the pystray backend usable on the target platform."""

import sys

if sys.platform == "win32":
    hiddenimports = ["pystray._win32"]
    excludedimports = [
        "pystray._appindicator",
        "pystray._darwin",
        "pystray._gtk",
        "pystray._xorg",
    ]
elif sys.platform == "darwin":
    hiddenimports = ["pystray._darwin"]
    excludedimports = [
        "pystray._appindicator",
        "pystray._gtk",
        "pystray._win32",
        "pystray._xorg",
    ]
else:
    hiddenimports = ["pystray._xorg"]
    excludedimports = [
        "pystray._appindicator",
        "pystray._darwin",
        "pystray._gtk",
        "pystray._win32",
        "gi",
    ]
