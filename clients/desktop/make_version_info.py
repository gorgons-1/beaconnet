"""Write version_info.txt (Windows file properties: product name and version) for PyInstaller.
SignPath requires the product name and version to be set on every signed binary."""

import re
import sys
from pathlib import Path

here = Path(__file__).parent
version = re.search(r'^VERSION = "([0-9.]+)"', (here / "beaconnet_desktop.py").read_text(), re.M).group(1)
parts = (version.split(".") + ["0", "0", "0"])[:4]
nums = ", ".join(str(int(p)) for p in parts)
dotted = ".".join(parts)
text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({nums}), prodvers=({nums}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'BeaconNet contributors'),
      StringStruct('FileDescription', 'BeaconNet Desktop'),
      StringStruct('FileVersion', '{dotted}'),
      StringStruct('InternalName', 'BeaconNet'),
      StringStruct('LegalCopyright', 'MIT licence, BeaconNet contributors'),
      StringStruct('OriginalFilename', 'BeaconNet.exe'),
      StringStruct('ProductName', 'BeaconNet Desktop'),
      StringStruct('ProductVersion', '{dotted}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
out = Path(sys.argv[1] if len(sys.argv) > 1 else here) / "version_info.txt"
out.write_text(text)
print(f"{out}: BeaconNet Desktop {dotted}")
