#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Each script called the way PartCAD's wrapper calls it, with the real CADCraft, outside PartCAD.

'build(request)' for the part type, 'process(path, request)' for the reader and
the renderer, each with the request PartCAD would send. CADCraft is fetched as a
sandbox fetches it, and these skip where it cannot be.
"""

import math
import os
import time

import pytest
from conftest import HERE, plugin_module

pytest.importorskip("cadquery")
b3d = pytest.importorskip("build123d")
ezdxf = pytest.importorskip("ezdxf")

INCH = 25.4
PLATE_VOLUME = 3 * (4 * INCH * 2 * INCH - 2 * math.pi * (0.25 * INCH) ** 2)


def sample(name):
    return os.path.join(HERE, name)


def contents(path, tmp_path):
    """What a drawing holds, as {(entity type, layer): count}, read through CADCraft and ezdxf."""
    tool = plugin_module("cadcraft_tool")
    if path.lower().endswith(".dwg"):
        dxf = str(tmp_path / (os.path.basename(path) + ".dxf"))
        tool.convert(path, dxf)
        path = dxf
    counts = {}
    for entity in ezdxf.readfile(path).modelspace():
        key = (entity.dxftype(), entity.dxf.layer)
        counts[key] = counts.get(key, 0) + 1
    return counts


@pytest.mark.parametrize("file", ["plate.dwg", "plate.dxf"])
def test_the_part_type_builds_the_plate(cadcraft, file):
    from cadquery.occ_impl.shapes import Shape

    script = plugin_module("read_part")
    request = {"path": sample(file), "config": {}, "parameters": {"thickness": 3, "excludeLayers": "BENDS"}}
    solid = Shape.cast(script.build(request))
    assert solid.isValid()
    assert solid.Volume() == pytest.approx(PLATE_VOLUME)


def test_the_part_type_falls_back_to_the_part_s_own_fields(cadcraft):
    from cadquery.occ_impl.shapes import Shape

    script = plugin_module("read_part")
    request = {"path": sample("plate.dwg"), "config": {"includeLayers": ["OUTLINE"], "thickness": 1}, "parameters": {}}
    assert Shape.cast(script.build(request)).Volume() == pytest.approx(4 * INCH * 2 * INCH)


def read_kit(file, tmp_path, **parameters):
    reader = plugin_module("read_assembly")
    request = {"source_file": sample(file), "output_folder": str(tmp_path / "out"), "object_name": "kit"}
    request.update(parameters)
    return reader.process(str(tmp_path), request)


def summary(node):
    """A node, as (type, name, its placement rounded, its children) -- what the tests compare."""
    location = node.get("location")
    placed = None
    if location:
        placed = (tuple(round(v, 6) for v in location[0]), round(location[2], 6))
    return (node["type"], node["name"], placed, [summary(child) for child in node.get("links", [])])


@pytest.mark.parametrize("file", ["kit.dwg", "kit.dxf"])
def test_each_block_is_a_part_and_each_reference_an_instance(cadcraft, tmp_path, file):
    result = read_kit(file, tmp_path)
    assert summary(result["root"]) == (
        "assembly",
        "kit",
        None,
        [
            ("part", "BRACKET", ((10, 10, 0), 0), []),
            ("part", "BRACKET", ((150, 10, 0), 90), []),
            (
                "assembly",
                "FRAME",
                ((50, 60, 0), 0),
                [
                    ("part", "FRAME", None, []),
                    ("part", "PIN", ((20, 10, 0), 0), []),
                    ("part", "PIN", ((80, 10, 0), 0), []),
                ],
            ),
            ("part", "PIN_scaled_2", ((180, 80, 0), 0), []),
            ("part", "BRACKET_transformed", None, []),
            ("part", "BASE", None, []),
        ],
    )
    assert result["dropped"] == {"text": 1, "layer": 1}
    assert result["info"]["Blocks"] == 3
    assert result["info"]["BlockReferences"] == 7


def test_a_part_is_its_block_in_its_own_coordinates(cadcraft, tmp_path):
    result = read_kit("kit.dwg", tmp_path, thickness=2)
    files = {}

    def collect(node):
        if node["type"] == "part":
            files[node["name"]] = node["part_file"]
        for child in node.get("links", []):
            collect(child)

    collect(result["root"])
    bracket = b3d.import_brep(files["BRACKET"])
    assert bracket.volume == pytest.approx(2 * (40 * 30 - math.pi * 16))
    assert tuple(bracket.bounding_box().min) == pytest.approx((0, 0, 0))
    assert b3d.import_brep(files["PIN_scaled_2"]).volume == pytest.approx(2 * math.pi * 36)
    # The mirrored reference keeps its whole transform: x from 100 - 40 to 100.
    mirrored = b3d.import_brep(files["BRACKET_transformed"]).bounding_box()
    assert (mirrored.min.X, mirrored.max.X) == pytest.approx((60, 100))


def test_the_assembly_reads_only_the_layers_asked_for(cadcraft, tmp_path):
    names = [link["name"] for link in read_kit("kit.dwg", tmp_path, excludeLayers=["parts"])["root"]["links"]]
    assert names == ["BASE"]


def test_a_sketch_renders_as_its_own_edges(cadcraft, tmp_path):
    renderer = plugin_module("render_drawing")
    with b3d.BuildSketch() as sketch:
        b3d.Rectangle(40, 20, align=b3d.Align.MIN)
        with b3d.Locations((10, 10)):
            b3d.Circle(3, mode=b3d.Mode.SUBTRACT)
    path = str(tmp_path / "sketch.dwg")
    result = renderer.process(path, {"wrapped": sketch.sketch.wrapped, "shape_kind": "sketch"})
    assert result["success"], result["exception"]
    held = contents(path, tmp_path)
    assert held[("CIRCLE", "Visible")] == 1  # a circle, not twenty segments of one
    assert held[("LINE", "Visible")] == 4


def test_a_part_is_projected_with_hidden_lines_on_request(cadcraft, tmp_path):
    renderer = plugin_module("render_drawing")
    part = b3d.Box(10, 10, 10) - b3d.Cylinder(2, 10)
    request = {
        "wrapped": part.wrapped,
        "shape_kind": "part",
        "viewport_origin": [100, -100, 100],
        "viewport_up": [0, 0, 1],
        "hidden_lines": True,
    }
    path = str(tmp_path / "part.dxf")
    assert renderer.process(path, request)["success"]
    assert {"Visible", "Hidden"} <= {layer for _type, layer in contents(path, tmp_path)}


@pytest.mark.parametrize(
    "file, request_fields, header",
    [
        ("a.dwg", {"dwgVersion": 2000}, b"AC1015"),
        ("a.dwg", {"dwgVersion": "R2013"}, b"AC1027"),
        ("a.dxf", {"dxfVersion": 2007, "binary": True}, b"AutoCAD Binary DXF"),
    ],
)
def test_the_version_and_binary_dxf_are_cadcraft_s(cadcraft, tmp_path, file, request_fields, header):
    renderer = plugin_module("render_drawing")
    path = str(tmp_path / file)
    request = {"wrapped": b3d.Box(10, 20, 5).wrapped, "shape_kind": "part", **request_fields}
    assert renderer.process(path, request)["success"]
    with open(path, "rb") as f:
        assert f.read(len(header)) == header


def test_a_version_cadcraft_cannot_write_is_refused(cadcraft, tmp_path):
    renderer = plugin_module("render_drawing")
    result = renderer.process(str(tmp_path / "a.dwg"), {"wrapped": b3d.Box(1, 1, 1).wrapped, "dwgVersion": 14})
    assert not result["success"]
    assert "2000, 2004" in result["exception"]


@pytest.mark.parametrize("file", ["a.dwg", "a.dxf"])
def test_a_reproducible_render_is_the_same_bytes_every_time(cadcraft, tmp_path, file):
    renderer = plugin_module("render_drawing")
    request = {"wrapped": b3d.Box(10, 20, 5).wrapped, "shape_kind": "part", "reproducible": True}
    first, second = str(tmp_path / ("1" + file)), str(tmp_path / ("2" + file))
    renderer.process(first, request)
    time.sleep(1.1)  # across a second boundary, which is where a clock would show
    renderer.process(second, request)
    assert open(first, "rb").read() == open(second, "rb").read()
