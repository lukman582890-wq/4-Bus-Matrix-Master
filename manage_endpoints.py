"""
Manage Windows Sound Output (MMDevices) Friendly Name for 4-Bus Matrix Mixer.
Exposes a single unified Windows Sound Output endpoint:
    "4-Bus Matrix Master (4-Bus Matrix Audio)"
which feeds all 4 configurable soundcard output channels (e.g., Studio 26c Out 1, 2, 3, 4).
"""

from __future__ import annotations

import sys
import winreg
from typing import Dict, Tuple

PKEY_DEVICE_DESC = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
PKEY_IFACE_FRIENDLY = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"
RENDER_ROOT = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"

# Only 1 clean virtual master device in Windows Sound Output (Win+Ctrl+V);
# restore In 4 & In 5 to their original Voicemeeter names to avoid confusion.
CUSTOM_ENDPOINTS: Dict[str, Tuple[str, str]] = {
    "{5e5369b7-e021-4354-8f01-6d7fe4307a3b}": ("4-Bus Matrix Master", "4-Bus Matrix Audio"),
    "{6003ca06-1300-4adf-b908-7458c15b3664}": ("Voicemeeter In 4", "VB-Audio Voicemeeter VAIO"),
    "{7301b1af-9e65-43aa-bb71-d42e8054db4d}": ("Voicemeeter In 5", "VB-Audio Voicemeeter VAIO"),
}

ORIGINAL_ENDPOINTS: Dict[str, Tuple[str, str]] = {
    "{5e5369b7-e021-4354-8f01-6d7fe4307a3b}": ("Voicemeeter In 3", "VB-Audio Voicemeeter VAIO"),
    "{6003ca06-1300-4adf-b908-7458c15b3664}": ("Voicemeeter In 4", "VB-Audio Voicemeeter VAIO"),
    "{7301b1af-9e65-43aa-bb71-d42e8054db4d}": ("Voicemeeter In 5", "VB-Audio Voicemeeter VAIO"),
}


def apply_endpoint_names(restore: bool = False) -> int:
    mapping = ORIGINAL_ENDPOINTS if restore else CUSTOM_ENDPOINTS
    updated = 0
    for guid, (desc, adapter) in mapping.items():
        subkey = rf"{RENDER_ROOT}\{guid}\Properties"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, PKEY_DEVICE_DESC, 0, winreg.REG_SZ, desc)
                winreg.SetValueEx(k, PKEY_IFACE_FRIENDLY, 0, winreg.REG_SZ, adapter)
                updated += 1
        except FileNotFoundError:
            continue
    return updated


if __name__ == "__main__":
    is_restore = "--restore" in sys.argv
    count = apply_endpoint_names(restore=is_restore)
    mode_str = "Restored original names" if is_restore else "Applied 4-Bus Matrix Master endpoint"
    print(f"{mode_str} ({count} endpoints updated).")
