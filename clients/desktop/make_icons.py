"""Write icon.png / icon.ico / icon.icns for the packaged app (used by the build)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from beaconnet_desktop import tray_icon_image  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
big = tray_icon_image().resize((512, 512))
big.save(out / "icon.png")
big.save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
big.save(out / "icon.icns")
print("icons written to", out)
