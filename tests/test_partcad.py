#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""The package, as a workspace uses it: the package beside this file, through PartCAD.

Skipped where PartCAD is not installed, or is older than the file-backed
partTypes this plugin is built on. The tests that build something are marked
'slow': the first of them downloads CADCraft and builds PartCAD's sandbox.

The assemblies need one thing more, an `import:` reader handed a binary file as
it is; against a PartCAD that renders it as a template first they fail with a
UnicodeDecodeError, which is what the skip below checks for.
"""

import asyncio
import math
import os

import pytest
from conftest import HERE, PLUGIN

INCH = 25.4


def _partcad():
    """PartCAD, if it is installed and new enough to read a file through a partType."""
    pc = pytest.importorskip("partcad")
    if not hasattr(pc.output, "package_cache_dir"):
        pytest.skip("this PartCAD's partTypes cannot read a file; it is older than this plugin")
    return pc


@pytest.fixture(scope="module")
def ctx():
    pc = _partcad()
    return pc.Context(os.path.join(HERE, "partcad.yaml"))


def measured(ctx, shape):
    from partcad import shape_envelope

    metadata = asyncio.run(shape.get_metadata_async(ctx))
    assert metadata, getattr(shape, "errors", None)
    return shape_envelope.metadata_section(metadata, shape_envelope.METADATA_MEASUREMENTS)


def test_the_tests_are_not_part_of_the_plugin():
    pc = _partcad()
    pytest.importorskip("partcad_utils.pcignore")
    plugin = pc.Context(PLUGIN)
    names = [p["name"] if isinstance(p, dict) else p for p in plugin.get_all_packages(has_stuff=False)]
    assert not any("tests" in name for name in names)
    # ...and this folder is a workspace of its own, not a corner of the plugin's.
    assert pc.Context(HERE).root_path == HERE


def test_the_plugin_declares_its_types_drawings_and_application(ctx):
    plugin = ctx.get_project("//cadcraft")
    assert plugin is not None
    for name in ("dwg", "dxf"):
        assert plugin.get_part_type_config(name)["extension"] == name
        assert plugin.config_obj["import"][name]["kinds"] == ["assembly"]
        assert plugin.config_obj["render"][name]["extension"] == name
    # Drawings are output files, not geometry to go on modelling with.
    assert "export" not in plugin.config_obj
    assert plugin.config_obj["open"]["cadcraft"]["formats"] == ["dwg", "dxf"]


@pytest.mark.parametrize("name, file", [("plate", "plate.dwg"), ("plate_dxf", "plate.dxf")])
def test_the_file_is_the_workspace_s(ctx, name, file):
    part = ctx.get_part(":" + name)
    assert os.path.abspath(part.path) == os.path.join(HERE, file)
    assert os.path.abspath(part.path) in [os.path.abspath(p) for p in part.cache_dependencies]


def test_the_image_and_the_cache_are_the_plugin_s(ctx):
    pc = _partcad()
    plugin = ctx.get_project("//cadcraft")
    assert plugin.docker_image_declared.startswith("ghcr.io/partcad/partcad-cadcraft:")
    assert ctx.get_part(":plate").environment_cache_key.endswith(";image=" + plugin.docker_image_declared)
    assert pc.output.package_cache_dir(ctx, plugin) == os.path.join(PLUGIN, ".cache")


@pytest.mark.slow
@pytest.mark.parametrize("name", ["plate", "plate_dxf"])
def test_a_part_is_the_drawing_s_outline_extruded(ctx, name):
    hole = 0.25 * INCH
    found = measured(ctx, ctx.get_part(":" + name))
    assert found["volume"] == pytest.approx(3 * (4 * INCH * 2 * INCH - 2 * math.pi * hole * hole))
    assert found["bbox"] == pytest.approx([0, 0, 0, 4 * INCH, 2 * INCH, 3], abs=1e-6)


@pytest.mark.slow
def test_a_parameter_changes_the_part(ctx):
    assert measured(ctx, ctx.get_part(":plate;thickness=5"))["bbox"][5] == pytest.approx(5)


@pytest.mark.slow
@pytest.mark.parametrize("name", ["kit", "kit_dxf"])
def test_an_assembly_holds_each_block_once_per_reference(ctx, name):
    assembly = ctx.get_assembly(":" + name)
    try:
        asyncio.run(assembly.do_instantiate())
    except UnicodeDecodeError:
        pytest.skip("this PartCAD renders an import: reader's binary file as a template")
    names = [child.name for child in assembly.children]
    assert names == ["BRACKET", "BRACKET", "FRAME", "PIN_scaled_2", "BRACKET_transformed", "BASE"]
    # One part per block, however many times it is placed.
    assert assembly.children[0].item is assembly.children[1].item
    assert measured(ctx, ctx.get_part(":%s/BRACKET" % name))["volume"] == pytest.approx(2 * (40 * 30 - math.pi * 16))


@pytest.mark.slow
@pytest.mark.parametrize(
    # A 2018 DWG, and an ASCII DXF -- which CADCraft writes with CRLF line ends.
    "format_name, header",
    [("cadcraft:dwg", rb"AC1032"), ("cadcraft:dxf", rb"\s*0\r?\nSECTION")],
)
def test_anything_renders_as_a_drawing(ctx, tmp_path, format_name, header):
    import re

    for name, kind in ((":plate", "part"), (":outline", "sketch")):
        shape = ctx.get_part(name) if kind == "part" else ctx.get_sketch(name)
        target = tmp_path / ("%s.%s" % (name[1:], format_name.split(":")[1]))
        asyncio.run(shape.render_async(ctx, format_name=format_name, filepath=str(target)))
        assert target.is_file(), shape.errors
        with open(target, "rb") as f:
            assert re.match(header, f.read(32)), target
