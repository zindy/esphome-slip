#!/usr/bin/env python3
"""Prepare a local copy of ESPHome's `network` component that treats the SLIP
link as a network connection.

Why: ESPHome's api component drops every client while `network::is_connected()`
is false, and that header-only inline function only knows about WiFi, Ethernet,
modem and OpenThread. This script copies the installed `network` component to
<config-dir>/custom_components/network and adds a flag the slip component sets
when its netif is up.

The copy is made from the ESPHome that is installed in the *current* Python
environment, so it always matches the version you build with. Re-run it with
--force after upgrading ESPHome.

Usage:
    python3 patch_network_util.py [--config-dir DIR] [--check] [--force]
"""

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path

MARKER = "custom_interface_connected"
STAMP = ".patched-for-esphome"

DECLARATION = """namespace esphome::network {

/// Set by interfaces ESPHome doesn't know about (the custom SLIP component).
/// While true, is_connected() reports the node as connected.
inline bool &custom_interface_connected() {
  static bool connected = false;
  return connected;
}
"""

NAMESPACE_ANCHOR = "namespace esphome::network {\n"
RETURN_ANCHOR = (
    "#ifdef USE_HOST\n"
    "  return true;  // Assume it's connected\n"
    "#endif\n"
    "  return false;"
)
RETURN_PATCH = "  if (custom_interface_connected())\n    return true;\n\n"


def installed_network_dir() -> Path:
    spec = importlib.util.find_spec("esphome.components.network")
    if spec is None or not spec.submodule_search_locations:
        sys.exit("error: ESPHome is not installed in this Python environment "
                 "(activate your virtualenv first)")
    return Path(next(iter(spec.submodule_search_locations)))


def esphome_version() -> str:
    from esphome.const import __version__
    return __version__


def patch_util_h(text: str) -> str:
    if NAMESPACE_ANCHOR not in text or RETURN_ANCHOR not in text:
        sys.exit("error: util.h does not look like the ESPHome version this "
                 "patch was written for (2026.10.x); patch it by hand, see README")
    text = text.replace(NAMESPACE_ANCHOR, DECLARATION, 1)
    return text.replace(RETURN_ANCHOR, RETURN_PATCH + RETURN_ANCHOR, 1)


def copy_component(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for f in src.iterdir():
        if f.is_file() and f.suffix in {".py", ".h", ".cpp"}:
            shutil.copy2(f, dst / f.name)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config-dir", type=Path, default=Path.cwd(),
                    help="folder containing your ESPHome yaml (default: current dir)")
    ap.add_argument("--check", action="store_true",
                    help="only report the state, change nothing")
    ap.add_argument("--force", action="store_true",
                    help="re-copy the component from the installed ESPHome and re-patch")
    args = ap.parse_args()

    version = esphome_version()
    dst = args.config_dir / "custom_components" / "network"
    util_h = dst / "util.h"
    stamp = dst / STAMP

    patched = util_h.exists() and MARKER in util_h.read_text()
    stamped = stamp.read_text().strip() if stamp.exists() else None

    if args.check:
        print(f"ESPHome installed : {version}")
        print(f"local override    : {'present' if dst.exists() else 'missing'}")
        print(f"util.h patched    : {'yes' if patched else 'no'}")
        print(f"patched for       : {stamped or 'unknown'}")
        sys.exit(0 if patched and stamped == version else 1)

    if patched and stamped == version and not args.force:
        print(f"already patched for ESPHome {version}; nothing to do")
        return
    if patched and stamped != version and not args.force:
        sys.exit(f"error: override was made for ESPHome {stamped or 'unknown'}, "
                 f"installed is {version}. Re-run with --force")

    copy_component(installed_network_dir(), dst)
    util_h.write_text(patch_util_h(util_h.read_text()))
    stamp.write_text(version + "\n")
    print(f"patched {util_h} for ESPHome {version}")
    print("make sure external_components lists `network` next to `slip`")


if __name__ == "__main__":
    main()
