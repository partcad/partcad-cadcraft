#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""What has to agree across the package's files, and nothing else does keep in step."""

import os
import re
import shutil
import subprocess

import pytest
from conftest import PLUGIN, plugin_module


def read(name):
    with open(os.path.join(PLUGIN, name), encoding="utf-8") as f:
        return f.read()


def test_the_image_installs_the_release_the_scripts_download():
    """The Dockerfile and 'cadcraft_tool.py' each pin CADCraft, and must agree."""
    tool = plugin_module("cadcraft_tool")
    dockerfile = read("Dockerfile")
    assert "CADCRAFT_VERSION=%s;" % tool.VERSION in dockerfile
    for machine in ("x86_64", "aarch64"):
        sha256 = tool.PINNED_SHA256[tool.asset_name(tool.VERSION, "linux-" + machine)]
        assert "%s) CADCRAFT_ARCH=%s; SHA256=%s" % (machine, machine, sha256) in dockerfile


def test_both_image_pins_are_one_image():
    """The package's 'dockerImage' and the image 'pc ide open' runs CADCraft in."""
    pins = re.findall(r"(?:dockerImage|image): (ghcr\.io/partcad/partcad-cadcraft:\S+)", read("partcad.yaml"))
    assert len(pins) == 2
    assert len(set(pins)) == 1


@pytest.mark.skipif(shutil.which("sh") is None, reason="image-tag.sh is a shell script")
def test_the_pin_is_what_the_image_would_be_tagged():
    """The workflow refuses a pin that does not match what it builds; say so before a push does."""
    tag = subprocess.run(["sh", os.path.join(PLUGIN, "image-tag.sh")], capture_output=True, text=True, check=True)
    assert "ghcr.io/partcad/partcad-cadcraft:%s" % tag.stdout.strip() in read("partcad.yaml")
