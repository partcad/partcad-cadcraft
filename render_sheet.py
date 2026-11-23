#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Render a PartCAD object as a drawing sheet: views, dimensions and a title block, by CADCraft.

What 'render_drawing.py' writes is one projection of the object. This writes the
sheet somebody is handed to make the thing from:

* the **views**: a part or an assembly as its front, top and right views, lined
  up in the drawing's projection (third angle unless 'projection' says first),
  and an isometric one in the fourth corner; hidden edges dashed and centre
  marks on every hole seen end on. A sketch is drawn as the one view it is.
* the **dimensions**: the overall width and height of the front view, the depth
  across the side view, and the diameter of each size of hole a view sees end
  on -- CADCraft's own dimensions, in an ISO style, which stay associated with
  the lines they measure in the DWG or DXF.
* the **sheet**: an ISO A0-A4 paper-space layout with the ISO 5457 frame, a
  title block, and one viewport showing the views at the largest ISO 5455
  scale they fit at (or 'scale').

PartCAD projects the views, because CADCraft is a 2D drafting application with
no 3D model to project: build123d's exact hidden-line projection, as
'render_drawing.py' uses for its single view. Everything else is CADCraft's,
driven through 'cadcraft-cli run' one command at a time: the views are imported
into a new metric drawing, dimensioned in model space, and the sheet is laid
out in paper space and plotted (PDF, SVG, PNG) or saved (DWG, DXF -- see
'sheet' in 'partcad.yaml').

Run by PartCAD's export wrapper with the shape in 'request["wrapped"]', the file
to write as 'path', and 'request["cache_dir"]' to where 'cadcraft-cli' is kept.
"""

import json
import math
import os
import sys
import tempfile

# Siblings are imported by bare name, and the sandbox sets PYTHONSAFEPATH.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Raise with any change to what this produces (see PartCAD's 'code_version').
CACHE_VERSION = 1

# What a sheet is plotted as, and by which of CADCraft's plotters.
PLOTTED = {"pdf": "CADCraft PDF", "svg": "CADCraft SVG", "png": "CADCraft PNG"}
# What a sheet is saved as, layout and all.
SAVED = ("dwg", "dxf")

# ISO 216 A-series, as (long side, short side) in millimetres, and the name
# CADCraft's plotters give each paper (its sides short by long, whatever the
# orientation).
PAGES = {
    "a0": (1189.0, 841.0),
    "a1": (841.0, 594.0),
    "a2": (594.0, 420.0),
    "a3": (420.0, 297.0),
    "a4": (297.0, 210.0),
}
# A part on A3 and an assembly on A2 unless 'page' says otherwise, as
# SolidCraft's templates have it; landscape, except A4, which ISO 5457 holds
# upright.
DEFAULT_PAGES = {"part": "a3", "assembly": "a2", "sketch": "a3"}

# ISO 5457: the frame is 20 mm in from the edge the sheet is filed by and 10 mm
# from the others. The title block sits in its bottom right corner.
FILING_MARGIN = 20.0
MARGIN = 10.0
TITLE_BLOCK = (180.0, 40.0)

# ISO 5455's scales, largest first: the first the views fit the sheet at is the
# one used.
SCALES = (50, 20, 10, 5, 2, 1, 1 / 2, 1 / 5, 1 / 10, 1 / 20, 1 / 50, 1 / 100, 1 / 200, 1 / 500, 1 / 1000)

# Paper millimetres: between neighbouring views, around the whole of them, and
# from an edge to the dimension line measuring it -- whose text, above the line,
# needs about as much again.
GAP = 25.0
CLEARANCE = 8.0
DIMENSION_OFFSET = 10.0
DIMENSION_TEXT = 6.0

# The ISO 128 line widths, on paper: visible edges wide, the rest narrow.
WIDE = 0.5
NARROW = 0.25

# The views of a solid: (looking towards, up) for each, and the isometric one.
VIEWS = {
    "front": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "top": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
    "right": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "iso": ((1.0, -1.0, 1.0), (0.0, 0.0, 1.0)),
}
PROJECTIONS = ("first", "third")

# Holes are dimensioned once per size and view; this many dimensions of them at
# most, so that a perforated plate is not buried under its own diameters.
MAX_HOLE_DIMENSIONS = 12

DEFAULT_TIMEOUT = 600


def as_compound(shape):
    """A build123d Compound of whatever OCCT shape PartCAD handed over."""
    import build123d as b3d
    from OCP.BRep import BRep_Builder
    from OCP.TopAbs import TopAbs_COMPOUND
    from OCP.TopoDS import TopoDS, TopoDS_Compound

    if shape.ShapeType() == TopAbs_COMPOUND:
        return b3d.Compound(TopoDS.Compound_s(shape))
    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, shape)
    return b3d.Compound(compound)


# --------------------------------------------------------------------------- #
# Parameters                                                                  #
# --------------------------------------------------------------------------- #


def flag(value, default):
    """A yes-or-no parameter, read as PartCAD reads its own: "false" from a caller that is not YAML is false."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "on", "1")
    return bool(value)


def kind_of(request):
    kind = request.get("shape_kind") or "part"
    return {"scene": "assembly"}.get(kind, kind)


def page_of(request):
    """The page as (name, width, height, landscape), width and height as the sheet is laid out."""
    page = request.get("page")
    page = DEFAULT_PAGES.get(kind_of(request), "a3") if page in (None, "") else str(page).strip().lower()
    if page not in PAGES:
        raise ValueError(
            "'page' is %r; the sheets are ISO %s" % (request.get("page"), ", ".join(p.upper() for p in PAGES))
        )
    orientation = request.get("orientation")
    if orientation in (None, ""):
        landscape = page != "a4"
    else:
        orientation = str(orientation).strip().lower()
        if orientation not in ("landscape", "portrait"):
            raise ValueError("'orientation' is %r; it is landscape or portrait" % request.get("orientation"))
        landscape = orientation == "landscape"
    long_side, short_side = PAGES[page]
    width, height = (long_side, short_side) if landscape else (short_side, long_side)
    return page, width, height, landscape


def paper_name(page):
    """The paper as CADCraft's plotters name it: short side by long side, whichever way it is turned."""
    long_side, short_side = PAGES[page]
    return "ISO %s (%.2f x %.2f MM)" % (page.upper(), short_side, long_side)


def scale_of(value):
    """A scale as a factor, paper over model: 2, 0.5, "1:2", "2:1"; None to fit the sheet."""
    if value in (None, ""):
        return None
    if isinstance(value, str) and ":" in value:
        paper, model = value.split(":", 1)
        try:
            factor = float(paper) / float(model)
        except (ValueError, ZeroDivisionError):
            raise ValueError("'scale' is %r; give a number (2 for 2:1) or 'paper:model' text" % (value,))
    else:
        try:
            factor = float(value)
        except (TypeError, ValueError):
            raise ValueError("'scale' is %r; give a number (2 for 2:1) or 'paper:model' text" % (value,))
    if factor <= 0:
        raise ValueError("'scale' is %r; a scale is above nought" % (value,))
    return factor


def scale_text(factor):
    """'2:1', '1:5': a factor as a drawing writes it."""
    if factor >= 1:
        return "%g:1" % round(factor, 6)
    return "1:%g" % round(1 / factor, 6)


def projection_of(request):
    value = request.get("projection")
    if value in (None, ""):
        return "third"
    value = str(value).strip().lower()
    if value not in PROJECTIONS:
        raise ValueError("'projection' is %r; it is one of %s" % (request.get("projection"), ", ".join(PROJECTIONS)))
    return value


# --------------------------------------------------------------------------- #
# The views, as PartCAD projects them                                         #
# --------------------------------------------------------------------------- #


class View:
    """One view: its edges in its own plane's coordinates, and where it goes."""

    def __init__(self, name, visible, hidden):
        self.name = name
        self.visible = list(visible)
        self.hidden = list(hidden)
        self.dx = 0.0
        self.dy = 0.0

    def box(self):
        """(x0, y0, x1, y1) of the view where it is drawn."""
        import build123d as b3d

        bb = b3d.Compound(children=self.visible + self.hidden).bounding_box()
        return bb.min.X + self.dx, bb.min.Y + self.dy, bb.max.X + self.dx, bb.max.Y + self.dy

    @property
    def width(self):
        x0, _, x1, _ = self.box()
        return x1 - x0

    @property
    def height(self):
        _, y0, _, y1 = self.box()
        return y1 - y0

    def move_to(self, x, y):
        """Put the view's lower left corner at (x, y)."""
        x0, y0, _, _ = self.box()
        self.dx += x - x0
        self.dy += y - y0

    def placed(self, edges):
        import build123d as b3d

        return [b3d.Pos(self.dx, self.dy, 0) * edge for edge in edges]

    def circles(self):
        """The whole circles this view sees end on, as ((x, y), radius), where it is drawn: holes, shafts, bosses."""
        import build123d as b3d

        found = []
        for edge in self.visible + self.hidden:
            if edge.geom_type != b3d.GeomType.CIRCLE:
                continue
            if abs(edge.length - 2 * math.pi * edge.radius) > 1e-6 * max(1.0, edge.length):
                continue  # an arc: a fillet, or half a hole cut by the edge of the part
            centre = edge.arc_center
            found.append(((centre.X + self.dx, centre.Y + self.dy), edge.radius))
        unique = []
        for centre, radius in found:
            if not any(
                math.dist(centre, c) < 1e-6 * max(1.0, radius) and abs(radius - r) < 1e-6 * max(1.0, radius)
                for c, r in unique
            ):
                unique.append((centre, radius))
        return unique


def project(shape, name):
    direction, up = VIEWS[name]
    origin = tuple(1000.0 * d for d in direction)
    visible, hidden = shape.project_to_viewport(origin, up, look_at=(0, 0, 0))
    return View(name, visible, [] if name == "iso" else hidden)


def sketch_view(shape):
    """A sketch as the one view it is: its edges as they are when it lies in XY, else seen face on."""
    import build123d as b3d

    edges = list(shape.edges())
    if not edges:
        raise ValueError("there is nothing to draw: the sketch has no edges")
    box = shape.bounding_box()
    if box.max.Z - box.min.Z <= 1e-9 * max(1.0, box.diagonal):
        return View("front", edges, [])
    faces = shape.faces()
    if not faces:
        raise ValueError("this sketch does not lie in a plane CADCraft can be shown it in: it has no face to face")
    normal = faces[0].normal_at()
    up = b3d.Vector(0, 0, 1) if abs(normal.Z) < 0.9 else b3d.Vector(0, 1, 0)
    centre = box.center()
    visible, _hidden = shape.project_to_viewport(tuple(centre + normal * 1000.0), tuple(up), look_at=tuple(centre))
    return View("front", visible, [])


def views_of(request):
    """The views to draw, by name: a solid's four, or a sketch as the one it is."""
    shape = as_compound(request["wrapped"])
    if kind_of(request) == "sketch":
        return {"front": sketch_view(shape)}
    views = {name: project(shape, name) for name in VIEWS}
    if not views["front"].visible:
        raise ValueError("there is nothing to draw: the object has no edges")
    if not flag(request.get("iso"), True):
        del views["iso"]
    return views


def arrange(views, projection, factor):
    """Place the views in model space for a drawing at 'factor', and return their (x0, y0, x1, y1).

    Four cells, a row and a column for the front view's: the top view in its
    column, the side view in its row, and the isometric view in the corner left
    over. Third angle puts the top view above the front and the right view to
    its right; first angle puts them below and to the left. The views next to
    the front one are aligned on it, as projection lines would carry them.
    """
    gap = GAP / factor
    front = views["front"]
    if len(views) == 1:
        front.move_to(0.0, 0.0)
        return front.box()
    top, side, iso = views["top"], views["right"], views.get("iso")
    side_width = max(side.width, iso.width if iso else 0.0)
    top_height = max(top.height, iso.height if iso else 0.0)
    if projection == "third":
        front.move_to(0.0, 0.0)
        top.move_to(0.0, front.height + gap)
        side.move_to(front.width + gap, 0.0)
        if iso:
            iso.move_to(front.width + gap, front.height + gap)
    else:
        front.move_to(side_width + gap, top_height + gap)
        top.move_to(side_width + gap, top_height - top.height)
        side.move_to(side_width - side.width, top_height + gap)
        if iso:
            iso.move_to(side_width - iso.width, top_height - iso.height)
    boxes = [view.box() for view in views.values()]
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


def room(views, projection, factor):
    """The paper the views take at 'factor', as (width, height): theirs, dimensions on one side, and a margin."""
    x0, y0, x1, y1 = arrange(views, projection, factor)
    extra = DIMENSION_OFFSET + DIMENSION_TEXT + 2 * CLEARANCE
    return (x1 - x0) * factor + extra, (y1 - y0) * factor + extra


def fitting_scale(views, projection, area):
    """The largest ISO 5455 scale the views fit 'area' (width, height on paper) at, and whether any does."""
    for factor in SCALES:
        width, height = room(views, projection, factor)
        if width <= area[0] and height <= area[1]:
            return factor, True
    return SCALES[-1], False


# --------------------------------------------------------------------------- #
# The model: the views as a DXF CADCraft imports                              #
# --------------------------------------------------------------------------- #


def write_views(views, path, factor, hidden_lines):
    """Write every view where it goes, on layers 'Visible', 'Hidden' and 'Center', as a DXF in millimetres."""
    import build123d as b3d

    exporter = b3d.ExportDXF(unit=b3d.Unit.MM)
    exporter.add_layer("Visible")
    exporter.add_layer("Hidden", line_type=b3d.LineType.DASHED)
    exporter.add_layer("Center", line_type=b3d.LineType.CENTER)
    overshoot = 2.0 / factor
    for view in views.values():
        exporter.add_shape(view.placed(view.visible), layer="Visible")
        if hidden_lines and view.hidden:
            exporter.add_shape(view.placed(view.hidden), layer="Hidden")
        if view.name == "iso":
            continue
        marks = []
        for (x, y), radius in view.circles():
            reach = radius + overshoot
            marks.append(b3d.Edge.make_line((x - reach, y), (x + reach, y)))
            marks.append(b3d.Edge.make_line((x, y - reach), (x, y + reach)))
        if marks:
            exporter.add_shape(marks, layer="Center")
    exporter.write(path)


# --------------------------------------------------------------------------- #
# CADCraft's commands                                                         #
# --------------------------------------------------------------------------- #


def dimensions(views, projection, factor):
    """The dimensions, as CADCraft commands: overall sizes, and each size of hole once per view."""
    offset = DIMENSION_OFFSET / factor
    below = projection == "third" or len(views) == 1
    commands = []

    def linear(p1, p2, at):
        commands.append(("dimlinear", {"p1": list(p1), "p2": list(p2), "at": list(at)}))

    front = views["front"]
    x0, y0, x1, y1 = front.box()
    # Width under the front view (over it in first angle, where the top view is
    # under it), height on the side away from the side view.
    y = y0 - offset if below else y1 + offset
    linear((x0, y0 if below else y1), (x1, y0 if below else y1), ((x0 + x1) / 2, y))
    x = x0 - offset if projection == "third" or len(views) == 1 else x1 + offset
    linear((x0 if x < x0 else x1, y0), (x0 if x < x0 else x1, y1), (x, (y0 + y1) / 2))
    side = views.get("right")
    if side is not None:
        sx0, sy0, sx1, sy1 = side.box()
        y = sy0 - offset if below else sy1 + offset
        linear((sx0, sy0 if below else sy1), (sx1, sy0 if below else sy1), ((sx0 + sx1) / 2, y))

    holes = 0
    for name in ("front", "top", "right"):
        view = views.get(name)
        if view is None:
            continue
        sizes = set()
        for (x, y), radius in view.circles():
            size = round(2 * radius, 6)
            if size in sizes or holes >= MAX_HOLE_DIMENSIONS:
                continue
            sizes.add(size)
            holes += 1
            # The leader leaves at 45 degrees, up and to the right.
            commands.append(
                ("dimdiameter", {"center": [x, y], "point": [x + radius * math.sqrt(0.5), y + radius * math.sqrt(0.5)]})
            )
    return commands


def title_block(origin, request, scale, projection, page, kind):
    """The title block's lines and text, as CADCraft commands, with its lower left corner at 'origin'.

    The fields ISO 7200 asks for, laid out in four rows: who owns it and what it
    is made of; who drew it, who approved it and when; what it is called; and
    its number, revision, scale, projection and sheet.
    """
    ox, oy = origin
    width, height = TITLE_BLOCK
    rows = (0.0, 10.0, 22.0, 31.0, height)
    title = str(request.get("title") or request.get("shape_name") or "Drawing")
    cells = [
        # (row, x from, x to, label, value, value height)
        (0, 0, 80, "Identification number", request.get("number"), 3.5),
        (0, 80, 105, "Revision", request.get("revision"), 3.5),
        (0, 105, 130, "Scale", scale, 3.5),
        # A sketch is one view, which no projection relates to another.
        (0, 130, 160, "Projection", None if kind == "sketch" else "%s angle" % projection.capitalize(), 2.5),
        (0, 160, 180, "Sheet", "1/1", 3.5),
        (1, 0, 150, "Title", title, 5.0),
        (1, 150, 180, "Size", page.upper(), 3.5),
        (2, 0, 60, "Created by", request.get("drawn_by"), 3.0),
        (2, 60, 120, "Approved by", request.get("approved_by"), 3.0),
        (2, 120, 180, "Date of issue", request.get("date"), 3.0),
        (3, 0, 90, "Legal owner", request.get("company"), 3.0),
        (3, 90, 180, "Material", request.get("material"), 3.0),
    ]
    commands = [("rectang", {"p1": [ox, oy], "p2": [ox + width, oy + height]})]
    for y in rows[1:-1]:
        commands.append(("line", {"points": [[ox, oy + y], [ox + width, oy + y]]}))
    for row, x_from, x_to, label, value, size in cells:
        bottom, top = oy + rows[row], oy + rows[row + 1]
        if x_from > 0:
            commands.append(("line", {"points": [[ox + x_from, bottom], [ox + x_from, top]]}))
        commands.append(("text", {"at": [ox + x_from + 1.0, top - 1.0], "text": label, "height": 1.8, "justify": "TL"}))
        if value not in (None, ""):
            commands.append(("text", {"at": [ox + x_from + 1.5, bottom + 1.5], "text": str(value), "height": size}))
    return commands


def script(request, views, file_format, target, model):
    """Every CADCraft command, in order, from an empty metric drawing to the file written. Returns (commands, scale)."""
    page, width, height, landscape = page_of(request)
    projection = projection_of(request)
    kind = kind_of(request)

    # The frame, the title block in its corner, and the paper above that block
    # left for the views.
    frame = (FILING_MARGIN, MARGIN, width - MARGIN, height - MARGIN)
    area = (frame[2] - frame[0], frame[3] - frame[1] - TITLE_BLOCK[1])
    factor = scale_of(request.get("scale"))
    fitted = True
    if factor is None:
        factor, fitted = fitting_scale(views, projection, area)
    width_needed, height_needed = room(views, projection, factor)
    x0, y0, x1, y1 = arrange(views, projection, factor)
    warnings = []
    if not fitted or width_needed > area[0] or height_needed > area[1]:
        warnings.append(
            "the views do not fit an %s sheet at %s; name a larger 'page' or a smaller 'scale'"
            % (page.upper(), scale_text(factor))
        )
    write_views(views, model, factor, flag(request.get("hidden_lines"), True))

    commands = [
        ("import", {"path": model}),
        # Dashes and centre lines a few millimetres long on paper, whatever the
        # scale: PSLTSCALE scales a pattern by the viewport's scale, and this
        # brings the inch-sized DASHED and CENTER down to ISO 128's proportions.
        ("ltscale", {"scale": 0.25}),
        ("setvar", {"name": "PSLTSCALE", "value": 1}),
        ("layer.set", {"name": "Visible", "lineweight": WIDE}),
        ("layer.set", {"name": "Hidden", "lineweight": NARROW}),
        ("layer.set", {"name": "Center", "lineweight": NARROW}),
        ("layer.new", {"name": "Dimensions", "lineweight": NARROW, "current": True}),
        # The ISO style a metric drawing starts on, sized for the paper: its
        # 2.5 mm text and arrows are paper millimetres at any scale.
        ("dimstyle", {"name": "ISO-25", "current": True, "DIMSCALE": 1 / factor, "DIMTXT": 3.5, "DIMASZ": 3.0}),
    ]
    if flag(request.get("dimensions"), True):
        commands += dimensions(views, projection, factor)

    # The sheet: a layout of its own, its page set up, the frame and the title
    # block in paper space, and one viewport onto the views at the scale.
    commands += [
        ("layout.new", {"name": "Sheet", "viewport": False}),
        ("layout.set", {"name": "Sheet"}),
        ("pagesetup", {"paper": paper_name(page), "landscape": landscape, "margins": [0, 0, 0, 0], "scale": 1}),
        ("layer.new", {"name": "Frame", "lineweight": 0.7, "current": True}),
        ("rectang", {"p1": [frame[0], frame[1]], "p2": [frame[2], frame[3]]}),
        ("layer.new", {"name": "Title block", "lineweight": 0.35, "current": True}),
    ]
    commands += title_block((frame[2] - TITLE_BLOCK[0], frame[1]), request, scale_text(factor), projection, page, kind)
    # The viewport fills the frame, so its own border -- which is on a layer that
    # does not plot anyway -- is nowhere to be seen; the views are centred on the
    # paper above the title block.
    area_centre = ((frame[0] + frame[2]) / 2, frame[1] + TITLE_BLOCK[1] + area[1] / 2)
    frame_centre = ((frame[0] + frame[2]) / 2, (frame[1] + frame[3]) / 2)
    views_centre = ((x0 + x1) / 2, (y0 + y1) / 2)
    centre = [
        views_centre[0] + (frame_centre[0] - area_centre[0]) / factor,
        views_centre[1] + (frame_centre[1] - area_centre[1]) / factor,
    ]
    commands += [
        ("layer.new", {"name": "Viewports", "current": True}),
        ("layer.set", {"name": "Viewports", "plot": False}),
        ("mview", {"p1": [frame[0], frame[1]], "p2": [frame[2], frame[3]]}),
        ("viewport.set", {"handle": "{last}", "scale": factor, "center": centre, "locked": True}),
        ("layer.current", {"name": "0"}),
    ]

    if file_format in PLOTTED:
        commands.append(
            (
                "plot",
                {
                    "path": target,
                    "layout": "Sheet",
                    "device": PLOTTED[file_format],
                    "paper": paper_name(page),
                    "landscape": landscape,
                    "scale": 1,
                    "lineweights": True,
                },
            )
        )
    else:
        params = {"path": target}
        version = request.get("dwgVersion" if file_format == "dwg" else "dxfVersion")
        if version not in (None, ""):
            params["version"] = str(version).strip().upper().lstrip("R")
        if file_format == "dxf" and flag(request.get("binary"), False):
            params["binary"] = True
        commands.append(("saveas", params))
    return commands, factor, warnings


def arguments(commands):
    """The 'cadcraft-cli run' arguments for 'commands'."""
    args = ["run", "--metric"]
    for name, params in commands:
        args += ["--cmd", "%s %s" % (name, json.dumps(params))]
    return args


def process(path, request):
    try:
        import cadcraft_tool

        if request.get("wrapped") is None:
            raise ValueError("No shape to draw")
        target = os.path.abspath(path)
        file_format = os.path.splitext(target)[1].lstrip(".").lower()
        if file_format not in PLOTTED and file_format not in SAVED:
            raise ValueError(
                "a sheet is written as %s, not '%s'" % (", ".join(list(PLOTTED) + list(SAVED)).upper(), file_format)
            )
        views = views_of(request)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="partcad-cadcraft-sheet-") as workdir, cadcraft_tool.staged(
            target
        ) as written:
            model = os.path.join(workdir, "views.dxf")
            commands, _factor, warnings = script(request, views, file_format, written, model)
            completed = cadcraft_tool.run(
                arguments(commands),
                cache_dir=request.get("cache_dir"),
                timeout=float(request.get("cadcraftTimeout") or cadcraft_tool.DEFAULT_TIMEOUT),
            )
        if not os.path.isfile(target) or os.path.getsize(target) == 0:
            raise RuntimeError("CADCraft wrote no %s file: %s" % (file_format.upper(), target))
        said = [line for line in cadcraft_tool._warnings(completed) if not line.startswith("[")]
        return {"success": True, "exception": None, "warnings": warnings + ["CADCraft: %s" % line for line in said]}
    except Exception as e:
        return {"success": False, "exception": str(e)}
