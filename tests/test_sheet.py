#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""The drawing sheet ('render_sheet.py'): how it is laid out, and what CADCraft makes of it.

The layout -- which page, which scale, where each view goes and what is
dimensioned -- is worked out here before CADCraft is asked anything, and is
checked on its own. The tests taking the 'cadcraft' fixture run the real
CADCraft, fetched as a sandbox fetches it, and look at what it wrote.
"""

import os
import time

import pytest
from conftest import plugin_module

b3d = pytest.importorskip("build123d")
ezdxf = pytest.importorskip("ezdxf")


@pytest.fixture(scope="module")
def sheet():
    return plugin_module("render_sheet")


def plate():
    """90 x 60 x 8, with two 8 mm holes through it."""
    return b3d.Box(90, 60, 8) - b3d.Pos(30, 15, 0) * b3d.Cylinder(4, 20) - b3d.Pos(-30, -15, 0) * b3d.Cylinder(4, 20)


def bracket():
    """An angle: an 80 x 40 x 6 foot with a 12 mm hole, and a 40 mm upright with a 10 mm one."""
    shape = b3d.Box(80, 40, 6) + b3d.Pos(-37, 0, 20) * b3d.Box(6, 40, 40)
    return (
        shape - b3d.Pos(10, 0, 0) * b3d.Cylinder(6, 20) - b3d.Pos(-37, 0, 28) * b3d.Rot(0, 90, 0) * b3d.Cylinder(5, 20)
    )


def request_for(shape, **fields):
    return {"wrapped": shape.wrapped, "shape_name": "plate", "shape_kind": "part", **fields}


# --------------------------------------------------------------------------- #
# The layout, without CADCraft                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "fields, page",
    [
        ({}, ("a3", 420.0, 297.0, True)),
        ({"shape_kind": "assembly"}, ("a2", 594.0, 420.0, True)),
        ({"page": "A4"}, ("a4", 210.0, 297.0, False)),  # upright, as ISO 5457 holds A4
        ({"page": "a4", "orientation": "landscape"}, ("a4", 297.0, 210.0, True)),
        ({"page": "A1", "orientation": "Portrait"}, ("a1", 594.0, 841.0, False)),
    ],
)
def test_the_page_is_the_kind_s_unless_named(sheet, fields, page):
    assert sheet.page_of({"shape_kind": "part", **fields}) == page


def test_cadcraft_s_plotters_name_a_paper_short_side_first(sheet):
    assert sheet.paper_name("a3") == "ISO A3 (297.00 x 420.00 MM)"
    assert sheet.paper_name("a0") == "ISO A0 (841.00 x 1189.00 MM)"


@pytest.mark.parametrize(
    "reader, fields, message",
    [
        ("page_of", {"page": "letter"}, "ISO A0, A1, A2, A3, A4"),
        ("page_of", {"orientation": "sideways"}, "landscape or portrait"),
        ("projection_of", {"projection": "oblique"}, "first, third"),
        ("scale_of", {"scale": "huge"}, "paper:model"),
        ("scale_of", {"scale": 0}, "above nought"),
    ],
)
def test_what_cannot_be_drawn_is_refused_saying_why(sheet, reader, fields, message):
    request = {"shape_kind": "part", **fields}
    with pytest.raises(ValueError, match=message):
        if reader == "scale_of":
            sheet.scale_of(request["scale"])
        else:
            getattr(sheet, reader)(request)


@pytest.mark.parametrize(
    "value, factor, text", [(2, 2.0, "2:1"), (0.5, 0.5, "1:2"), ("1:5", 0.2, "1:5"), ("5:1", 5.0, "5:1")]
)
def test_a_scale_is_paper_over_model(sheet, value, factor, text):
    assert sheet.scale_of(value) == pytest.approx(factor)
    assert sheet.scale_text(sheet.scale_of(value)) == text


def test_third_angle_puts_the_top_view_above_and_the_right_view_to_the_right(sheet):
    views = sheet.views_of(request_for(plate()))
    sheet.arrange(views, "third", 1.0)
    front, top, side = views["front"].box(), views["top"].box(), views["right"].box()
    assert top[1] > front[3] and top[0] == pytest.approx(front[0])  # above, in line
    assert side[0] > front[2] and side[1] == pytest.approx(front[1])  # to the right, in line
    assert views["iso"].box()[0] > front[2] and views["iso"].box()[1] > front[3]  # the corner left over


def test_first_angle_puts_them_below_and_to_the_left(sheet):
    views = sheet.views_of(request_for(plate()))
    sheet.arrange(views, "first", 1.0)
    front, top, side = views["front"].box(), views["top"].box(), views["right"].box()
    assert top[3] < front[1] and top[0] == pytest.approx(front[0])
    assert side[2] < front[0] and side[1] == pytest.approx(front[1])


def test_the_views_are_true_size_and_the_gap_is_paper(sheet):
    views = sheet.views_of(request_for(plate()))
    sheet.arrange(views, "third", 0.5)
    assert (views["front"].width, views["front"].height) == pytest.approx((90, 8), abs=1e-6)
    assert (views["right"].width, views["top"].height) == pytest.approx((60, 60), abs=1e-6)
    # 25 mm on paper between neighbours is 50 mm of model at 1:2.
    assert views["right"].box()[0] - views["front"].box()[2] == pytest.approx(50)


@pytest.mark.parametrize(
    "scale_by, page, expected",
    # A fifth of the size fits at five times the scale; A0 takes the plate at 5:1.
    [(1, "a3", 1.0), (0.2, "a3", 5.0), (10, "a3", 0.1), (1, "a0", 5.0)],
)
def test_the_scale_is_the_largest_iso_one_the_views_fit_at(sheet, scale_by, page, expected):
    shape = b3d.scale(plate(), scale_by)
    views = sheet.views_of(request_for(shape))
    _name, width, height, _landscape = sheet.page_of({"page": page})
    area = (width - sheet.FILING_MARGIN - sheet.MARGIN, height - 2 * sheet.MARGIN - sheet.TITLE_BLOCK[1])
    factor, fitted = sheet.fitting_scale(views, "third", area)
    assert fitted
    assert factor == pytest.approx(expected)


def test_holes_are_dimensioned_once_per_size_and_view(sheet):
    views = sheet.views_of(request_for(plate()))
    sheet.arrange(views, "third", 1.0)
    commands = sheet.dimensions(views, "third", 1.0)
    names = [name for name, _params in commands]
    # Width and height of the front view, depth across the side one; one 8 mm hole of the two.
    assert names == ["dimlinear", "dimlinear", "dimlinear", "dimdiameter"]
    linear = [params for name, params in commands if name == "dimlinear"]
    measured = sorted(round(abs(p["p2"][0] - p["p1"][0]) + abs(p["p2"][1] - p["p1"][1]), 6) for p in linear)
    assert measured == [8, 60, 90]
    centre, point = commands[-1][1]["center"], commands[-1][1]["point"]
    assert ((point[0] - centre[0]) ** 2 + (point[1] - centre[1]) ** 2) ** 0.5 == pytest.approx(4)


def test_a_hole_is_dimensioned_in_the_view_that_sees_it_end_on(sheet):
    views = sheet.views_of(request_for(bracket()))
    sheet.arrange(views, "third", 1.0)
    diameters = {}
    for name in ("front", "top", "right"):
        diameters[name] = sorted(round(2 * r, 6) for _c, r in views[name].circles())
    assert diameters == {"front": [], "top": [12], "right": [10]}


def test_a_sketch_is_the_one_view_it_is(sheet):
    with b3d.BuildSketch(b3d.Plane.XZ) as sketch:  # not in XY: seen face on all the same
        b3d.Rectangle(60, 30)
        with b3d.Locations((15, 0)):
            b3d.Circle(5, mode=b3d.Mode.SUBTRACT)
    views = sheet.views_of({"wrapped": sketch.sketch.wrapped, "shape_kind": "sketch"})
    assert list(views) == ["front"]
    assert (views["front"].width, views["front"].height) == pytest.approx((60, 30), abs=1e-6)
    assert [round(2 * r, 6) for _c, r in views["front"].circles()] == [10]


def test_the_title_block_says_what_it_is_given(sheet):
    request = {"title": "Mounting Plate", "number": "PC-EX-0001", "drawn_by": "A. Engineer", "date": "2026-11-23"}
    texts = [
        p["text"] for name, p in sheet.title_block((0, 0), request, "1:1", "third", "a3", "part") if name == "text"
    ]
    for value in ("Mounting Plate", "PC-EX-0001", "A. Engineer", "2026-11-23", "1:1", "Third angle", "A3", "1/1"):
        assert value in texts
    # Without a title, the object's name.
    block = sheet.title_block((0, 0), {"shape_name": "plate"}, "1:1", "third", "a3", "part")
    texts = [p["text"] for name, p in block if name == "text"]
    assert "plate" in texts
    # A sketch is one view: no projection to state.
    texts = [p["text"] for name, p in sheet.title_block((0, 0), {}, "1:1", "third", "a3", "sketch") if "text" in p]
    assert "Third angle" not in texts


def test_the_script_lays_out_the_sheet_and_writes_the_file_asked_for(sheet, tmp_path):
    views = sheet.views_of(request_for(plate(), title="Mounting Plate"))
    model = str(tmp_path / "views.dxf")
    commands, factor, warnings = sheet.script(request_for(plate()), views, "svg", "/out/plate.svg", model)
    names = [name for name, _params in commands]
    assert (factor, warnings) == (1.0, [])
    assert names[0] == "import" and commands[0][1] == {"path": model}
    assert os.path.isfile(model)
    # The viewport set is the one 'mview' just made.
    assert names[names.index("mview") + 1] == "viewport.set"
    assert commands[names.index("viewport.set")][1]["handle"] == "{last}"
    assert commands[names.index("viewport.set")][1]["scale"] == 1.0
    assert commands[-1] == (
        "plot",
        {
            "path": "/out/plate.svg",
            "layout": "Sheet",
            "device": "CADCraft SVG",
            "paper": "ISO A3 (297.00 x 420.00 MM)",
            "landscape": True,
            "scale": 1,
            "lineweights": True,
        },
    )
    commands, _factor, _warnings = sheet.script(
        request_for(plate(), dwgVersion="R2013"), views, "dwg", "/out/plate.dwg", model
    )
    assert commands[-1] == ("saveas", {"path": "/out/plate.dwg", "version": "2013"})


def test_views_that_do_not_fit_are_drawn_all_the_same_and_said_so(sheet, tmp_path):
    views = sheet.views_of(request_for(plate()))
    _commands, factor, warnings = sheet.script(
        request_for(plate(), scale=10), views, "pdf", "/out/plate.pdf", str(tmp_path / "views.dxf")
    )
    assert factor == 10
    assert warnings and "10:1" in warnings[0]


def test_a_file_type_a_sheet_is_not_written_as_is_refused(sheet):
    result = sheet.process("/out/plate.step", request_for(plate()))
    assert not result["success"]
    assert "PDF, SVG, PNG, DWG, DXF" in result["exception"]


# --------------------------------------------------------------------------- #
# What CADCraft makes of it                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "file, header",
    [("plate.pdf", b"%PDF-"), ("plate.svg", b"<"), ("plate.png", b"\x89PNG"), ("plate.dwg", b"AC10")],
)
def test_a_sheet_is_written_in_every_format(cadcraft, sheet, tmp_path, file, header):
    path = tmp_path / file
    result = sheet.process(str(path), request_for(plate(), title="Mounting Plate"))
    assert result["success"], result["exception"]
    assert result["warnings"] == []
    assert path.read_bytes().startswith(header)
    if file.endswith(".svg"):
        assert b"<svg" in path.read_bytes()[:4096]


def test_a_saved_sheet_holds_the_views_the_dimensions_and_the_layout(cadcraft, tmp_path):
    renderer = plugin_module("render_drawing")
    path = tmp_path / "plate.dxf"
    # 'sheet: true' on the 'dxf' type is the sheet, saved.
    result = renderer.process(str(path), {**request_for(plate()), "sheet": True, "hidden_lines": True})
    assert result["success"], result["exception"]
    drawing = ezdxf.readfile(str(path))
    model = [(e.dxftype(), e.dxf.layer) for e in drawing.modelspace()]
    assert model.count(("DIMENSION", "Dimensions")) == 4
    assert {"Visible", "Hidden", "Center"} <= {layer for _type, layer in model}
    layout = drawing.layouts.get("Sheet")
    viewports = [e for e in layout if e.dxftype() == "VIEWPORT" and e.dxf.id > 1]
    assert len(viewports) == 1
    # The scale the title block states: 1:1, a view height in model units equal to the paper's.
    assert viewports[0].dxf.view_height == pytest.approx(viewports[0].dxf.height)
    assert not drawing.layers.get("Viewports").dxf.plot


def test_a_sheet_is_the_same_bytes_every_time(cadcraft, sheet, tmp_path):
    first, second = tmp_path / "1.pdf", tmp_path / "2.pdf"
    request = request_for(plate(), reproducible=True)
    assert sheet.process(str(first), request)["success"]
    time.sleep(1.1)  # across a second boundary, which is where a clock would show
    assert sheet.process(str(second), request)["success"]
    assert first.read_bytes() == second.read_bytes()
