#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Reading a drawing into geometry, from the DXF samples: what CADCraft hands over is DXF too."""

import math
import os

import pytest
from conftest import HERE, plugin_module

pytest.importorskip("cadquery")
ezdxf = pytest.importorskip("ezdxf")

INCH = 25.4


@pytest.fixture(scope="module")
def drawing():
    return plugin_module("cadcraft_drawing")


@pytest.fixture
def plate(drawing, tmp_path):
    document, warnings = drawing.load(os.path.join(HERE, "plate.dxf"), str(tmp_path))
    assert warnings == []  # a DXF ezdxf reads is read as it is; CADCraft is not asked
    return document


def test_the_drawing_s_unit_is_scaled_to_millimetres(drawing, plate):
    assert drawing.millimetres_per_unit(plate, "auto") == (INCH, "in")
    assert drawing.millimetres_per_unit(plate, "mm") == (1.0, "mm")
    with pytest.raises(ValueError, match="unknown 'units'"):
        drawing.millimetres_per_unit(plate, "furlong")


def test_block_references_explode_onto_the_reference_s_layer(drawing, plate):
    modelspace = plate.modelspace()
    drawing.explode_blocks(modelspace)
    # The circles live in the block on layer 0; AutoCAD draws them on 'HOLES'.
    assert sorted(e.dxf.layer for e in modelspace if e.dxftype() == "CIRCLE") == ["HOLES", "HOLES"]


def test_layers_read_together_make_one_face_with_holes(drawing, plate):
    modelspace = plate.modelspace()
    drawing.explode_blocks(modelspace)
    layers = drawing.Layers(exclude="bends, notes")
    found = drawing.wires([e for e in modelspace if e.dxf.layer in layers], 1e-6, INCH)
    shape, closed, warning = drawing.profile(found)
    assert (closed, warning) == (True, None)
    assert [len(face.innerWires()) for face in shape.Faces()] == [2]

    solid = drawing.extrude(shape, 3.0)
    hole = 0.25 * INCH
    assert solid.isValid()
    assert solid.Volume() == pytest.approx(3 * (4 * INCH * 2 * INCH - 2 * math.pi * hole * hole))


def test_open_lines_are_kept_as_wires_and_said_to_be(drawing, plate):
    bends = [e for e in plate.modelspace() if e.dxf.layer == "BENDS"]
    shape, closed, warning = drawing.profile(drawing.wires(bends, 1e-6, 1.0))
    assert not closed
    assert "kept the wires" in warning
    with pytest.raises(ValueError, match="no closed outline"):
        drawing.extrude(shape, 1.0)


def test_a_mirrored_circle_is_where_the_drawing_shows_it(drawing):
    # Upside down: its X runs the other way, so -10 in its own coordinates is
    # +10 in the drawing's. Read the right way up it would be at -10.
    circle = ezdxf.new().modelspace().add_circle((-10, 0), 2, dxfattribs={"extrusion": (0, 0, -1)})
    (wire,) = drawing.wires([circle], 1e-6, 1.0)
    assert wire.Center().x == pytest.approx(10)


def test_layers_are_selected_without_regard_to_case(drawing):
    assert "Outline" in drawing.Layers(include=["OUTLINE"])
    assert "Notes" not in drawing.Layers(exclude="notes")
    with pytest.raises(ValueError, match="not both"):
        drawing.Layers(include="a", exclude="b")


def test_what_is_not_geometry_is_counted(drawing, plate):
    assert drawing.count_dropped(plate.modelspace()) == {"text": 1}


@pytest.mark.parametrize(
    "attributes, expected",
    [
        ({}, ((5, 6, 0), 0.0, 1.0)),
        ({"rotation": 90}, ((5, 6, 0), 90.0, 1.0)),
        ({"rotation": 180}, ((5, 6, 0), 180.0, 1.0)),
        ({"rotation": -30, "xscale": 2, "yscale": 2}, ((5, 6, 0), 30.0, 2.0)),
    ],
)
def test_a_block_reference_is_a_placement(drawing, attributes, expected):
    document = ezdxf.new()
    document.blocks.new("B").add_line((0, 0), (1, 0))
    reference = document.modelspace().add_blockref("B", (5, 6), dxfattribs=attributes)
    translation, axis, degrees, scale = drawing.placement(reference.matrix44())
    assert translation == pytest.approx(expected[0])
    assert (degrees, scale) == pytest.approx(expected[1:])
    if degrees:
        # A turn the other way is the same angle about the axis the other way up.
        assert axis == pytest.approx((0, 0, -1 if attributes.get("rotation", 0) < 0 else 1))


@pytest.mark.parametrize("attributes", [{"xscale": -1}, {"xscale": 2, "yscale": 1}])
def test_a_mirrored_or_stretched_reference_is_no_placement(drawing, attributes):
    document = ezdxf.new()
    document.blocks.new("B").add_line((0, 0), (1, 0))
    reference = document.modelspace().add_blockref("B", (0, 0), dxfattribs=attributes)
    assert drawing.placement(reference.matrix44()) is None


def test_a_name_becomes_a_safe_unique_object_name(drawing):
    taken = set()
    assert drawing.node_name("BOLT", taken) == "BOLT"
    assert drawing.node_name("bolt", taken) == "bolt_2"
    assert drawing.node_name("Cut / 3 mm", taken) == "Cut_3_mm"
    assert drawing.safe_name("***") == "item"
