#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Render a PartCAD object as an AutoCAD drawing: DWG or DXF (the 'render:' entries).

A render, not an export, and deliberately so. In PartCAD an export is geometry
another CAD tool can go on working with; a render is an output file -- a
drawing, a picture. A DWG or DXF of a part is a *drawing* of it: its edges seen
from one direction, flat, with the hidden ones optionally dashed. Nothing in it
is the solid, and CADCraft cannot write a solid (an ACIS 3DSOLID) into either
format. So both file types are declared under 'render:', where PartCAD promises
an output file and nothing more.

* A **sketch** is drawn as the edges it is made of, untouched: it is already
  flat, so nothing is projected, and a sketch read from a DWG comes back out
  with its arcs, circles and coordinates as they were.
* A **part or an assembly** is projected through OCCT's exact hidden-line
  algorithm (build123d's 'project_to_viewport'), looking from 'viewport_origin'
  towards the origin with 'viewport_up' up -- a plan view unless the file type
  or 'pc render --view' says otherwise. Visible edges go on the layer 'Visible'
  and, with 'hidden_lines: true', hidden ones on a dashed layer 'Hidden'.

Lines, arcs, circles, ellipses and splines are written as what they are, not
flattened into straight segments (which is what PartCAD's built-in 'dxf' does,
going through its SVG projection). build123d writes the drawing as DXF, and
CADCraft saves it in the format and version asked for: 'dwgVersion' or
'dxfVersion' (2000, 2004, 2007, 2010, 2013 or 2018) and, for a DXF, 'binary'.

Run by PartCAD's export wrapper with the shape in 'request["wrapped"]', the file
to write as 'path', and 'request["cache_dir"]' to where 'cadcraft-cli' is kept.
"""

import os
import sys
import tempfile

# Siblings are imported by bare name, and the sandbox sets PYTHONSAFEPATH.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Raise with any change to what this produces (see PartCAD's 'code_version').
CACHE_VERSION = 1

DEFAULT_VIEWPORT_ORIGIN = (0, 0, 100)
DEFAULT_VIEWPORT_UP = (0, 1, 0)
LOOK_AT = (0, 0, 0)
VERSIONS = ("2000", "2004", "2007", "2010", "2013", "2018")


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


def edges(request):
    """(visible, hidden) edges of the drawing, in drawing coordinates."""
    shape = as_compound(request["wrapped"])
    if request.get("shape_kind") == "sketch":
        return list(shape.edges()), []
    origin = tuple(float(v) for v in (request.get("viewport_origin") or DEFAULT_VIEWPORT_ORIGIN))
    up = tuple(float(v) for v in (request.get("viewport_up") or DEFAULT_VIEWPORT_UP))
    visible, hidden = shape.project_to_viewport(origin, up, look_at=LOOK_AT)
    return list(visible), list(hidden)


def write_dxf(visible, hidden, path, request):
    """Write the drawing as a DXF with build123d, which keeps every curve a curve."""
    import build123d as b3d
    import ezdxf

    if not visible and not hidden:
        raise ValueError("there is nothing to draw: the object has no edges from this direction")
    # A DXF is stamped with the clock and a fresh pair of GUIDs on every save;
    # this writes fixed ones instead, as PartCAD's own DXF render does for a
    # reproducible drawing, and CADCraft carries them into what it saves.
    ezdxf.options.write_fixed_meta_data_for_testing = bool(request.get("reproducible", False))
    line_weight = request.get("line_weight")
    exporter = b3d.ExportDXF(unit=b3d.Unit.MM, line_weight=float(line_weight) if line_weight else None)
    exporter.add_layer("Visible")
    exporter.add_shape(visible, layer="Visible")
    if hidden and request.get("hidden_lines", False):
        exporter.add_layer("Hidden", line_type=b3d.LineType.DASHED)
        exporter.add_shape(hidden, layer="Hidden")
    exporter.write(path)


def version_of(request, key):
    value = request.get(key)
    if value in (None, ""):
        return None
    value = str(value).strip().upper().lstrip("R")
    if value not in VERSIONS:
        raise ValueError("'%s' is %r; CADCraft writes %s" % (key, request.get(key), ", ".join(VERSIONS)))
    return value


def process(path, request):
    try:
        import cadcraft_tool

        target = os.path.abspath(path)
        is_dwg = target.lower().endswith(".dwg")
        version = version_of(request, "dwgVersion" if is_dwg else "dxfVersion")
        binary = bool(request.get("binary", False)) and not is_dwg
        visible, hidden = edges(request)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="partcad-cadcraft-") as workdir:
            drawn = os.path.join(workdir, "drawing.dxf")
            write_dxf(visible, hidden, drawn, request)
            warnings = cadcraft_tool.save_as(
                drawn,
                target,
                version=version,
                binary=binary,
                cache_dir=request.get("cache_dir"),
                timeout=float(request.get("cadcraftTimeout") or cadcraft_tool.DEFAULT_TIMEOUT),
            )
        for line in warnings:
            sys.stderr.write("cadcraft: %s\n" % line)
        return {"success": True, "exception": None}
    except Exception as e:
        return {"success": False, "exception": str(e)}
