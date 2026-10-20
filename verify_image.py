#!/usr/bin/env python3
#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Whether this image can actually run CADCraft.

Run at build time, so that an image which cannot is never published. The base
image's own `verify.py` checks the contract PartCAD talks to a container
through; this checks what this image adds on top of it, the way the package
uses it:

* `cadcraft-cli` draws a drawing, saves it as a DWG and reads that back --
  headless, with no GPU and no display, which is how every script in this
  package runs it -- and finds in it what was drawn;
* every library the desktop application opens at run time rather than links
  can be opened, since that is what `pc ide open` running it in here needs and
  a missing one would only show as a window that never appears.
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile

# What the application loads with dlopen(): its display, its renderer and its
# file dialogs. The command line tool links none of it.
RUNTIME_LIBRARIES = (
    "libEGL.so.1",
    "libvulkan.so.1",
    "libwayland-client.so.0",
    "libwayland-egl.so.1",
    "libX11-xcb.so.1",
    "libxcb.so.1",
    "libXcursor.so.1",
    "libXi.so.6",
    "libxkbcommon.so.0",
    "libxkbcommon-x11.so.0",
    "libXrandr.so.2",
    "libdbus-1.so.3",
)


def main() -> int:
    failures = []

    for name in ("cadcraft-cli", "cadcraft"):
        if not shutil.which(name):
            failures.append("'%s' is not on PATH" % name)

    cli = shutil.which("cadcraft-cli")
    if cli:
        with tempfile.TemporaryDirectory() as tmp:
            dwg = os.path.join(tmp, "box.dwg")
            # A 10 x 20 rectangle and a circle, saved as a 2018 DWG.
            drawn = subprocess.run(
                [
                    cli,
                    "run",
                    "--metric",
                    "--cmd",
                    'rectang {"p1": [0, 0], "p2": [10, 20]}',
                    "--cmd",
                    'circle {"center": [5, 10], "radius": 2}',
                    "--cmd",
                    "saveas " + json.dumps({"path": dwg, "version": "2018"}),
                ],
                capture_output=True,
                text=True,
            )
            if drawn.returncode != 0 or not os.path.isfile(dwg):
                failures.append("cadcraft-cli could not save a DWG: %s" % (drawn.stderr.strip() or drawn.returncode))
            else:
                read = subprocess.run([cli, "info", dwg], capture_output=True, text=True)
                try:
                    counts = json.loads(read.stdout)["counts"]
                except (ValueError, KeyError):
                    counts = None
                if not counts or counts.get("Circle") != 1:
                    failures.append("cadcraft-cli read its own DWG back as %r, not a rectangle and a circle" % counts)
                else:
                    print("cadcraft-cli saved and read back a DWG holding %r" % counts)

    for library in RUNTIME_LIBRARIES:
        try:
            ctypes.CDLL(library)
        except OSError as e:
            failures.append("the application could not open %s: %s" % (library, e))

    for failure in failures:
        print("verify_image: %s" % failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
