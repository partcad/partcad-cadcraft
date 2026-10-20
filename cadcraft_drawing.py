#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Read an AutoCAD drawing -- a DWG or a DXF -- into OCCT geometry.

Shared by the part type ('read_part.py') and the assembly reader
('read_assembly.py').

**Getting the drawing.** A DWG is converted to DXF by CADCraft. A DXF is read
as it is, by ezdxf; one ezdxf cannot make sense of -- an old R12 file, a
binary one a tool wrote oddly -- goes through CADCraft first, which reads DXF
from R12 to 2018, ASCII and binary, and writes it back out clean.

**Reading it** is what PartCAD does with a DXF sketch: CadQuery's converters
turn every LINE, ARC, CIRCLE, ELLIPSE, SPLINE and (LW)POLYLINE into edges, the
edges are joined into wires, and closed wires into faces with their holes. On
top of that:

* **'$INSUNITS' is honoured.** PartCAD works in millimetres, so a drawing that
  says it is in inches is scaled by 25.4; 'units' overrides what it says.
* **What is not geometry is counted**: text, dimensions and hatches have no
  place in a solid, and are reported rather than passed over.
* **Block references** are what a drawing reuses geometry with -- a bolt hole,
  a symbol, a whole sub-drawing. The part type explodes them where they are;
  the assembly reader keeps them, as parts placed where the references put
  them. Either way an entity a block keeps on layer 0 is on the layer of the
  reference, which is what layer 0 in a block means to AutoCAD.

Runs in a PartCAD sandbox with CadQuery installed ('pythonRequirements' in
partcad.yaml).
"""

import math
import os

# Pinned before CadQuery loads OCP, and with it VTK's bundled expat: PartCAD's
# own DXF importer does the same (see its 'wrapper_import_dxf.py').
import pyexpat  # noqa: F401
import re

import ezdxf
from cadquery.occ_impl.geom import Vector
from cadquery.occ_impl.importers.dxf import DXF_CONVERTERS, _dxf_convert
from cadquery.occ_impl.shapes import Compound, Face, Shape, Solid, sortWiresByBuildOrder
from ezdxf.upright import upright

import cadcraft_tool

# Millimetres per drawing unit, by '$INSUNITS' code. 0 is "unitless", read as
# millimetres: that is what a PartCAD drawing is in.
MILLIMETRES_PER_INSUNITS = {
    0: 1.0,
    1: 25.4,  # inches
    2: 304.8,  # feet
    3: 1609344.0,  # miles
    4: 1.0,  # millimetres
    5: 10.0,  # centimetres
    6: 1000.0,  # metres
    7: 1000000.0,  # kilometres
    8: 0.0000254,  # microinches
    9: 0.0254,  # mils
    10: 914.4,  # yards
    11: 0.0000001,  # angstroms
    12: 0.000001,  # nanometres
    13: 0.001,  # microns
    14: 100.0,  # decimetres
    15: 10000.0,  # decametres
    16: 100000.0,  # hectometres
    21: 1200.0 / 3937.0 * 1000.0,  # US survey feet
}

# What 'units' may say instead of "whatever the drawing says".
MILLIMETRES_PER_UNIT = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "inch": 25.4, "ft": 304.8, "none": 1.0}

# How what is not geometry is counted. Anything neither geometry nor listed
# here is 'other'. A block reference is neither: it is structure.
NOT_GEOMETRY = {
    "TEXT": "text",
    "MTEXT": "text",
    "ATTRIB": "text",
    "ATTDEF": "text",
    "DIMENSION": "dimension",
    "ARC_DIMENSION": "dimension",
    "LARGE_RADIAL_DIMENSION": "dimension",
    "LEADER": "dimension",
    "MULTILEADER": "dimension",
    "MLEADER": "dimension",
    "TOLERANCE": "dimension",
    "HATCH": "hatch",
    "MPOLYGON": "hatch",
    "SOLID": "hatch",
    "WIPEOUT": "hatch",
}

# How deep blocks may be placed inside blocks before a drawing is taken to have
# blocks that refer to each other in a cycle.
MAX_BLOCK_DEPTH = 32


def as_list(value):
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(item) for item in value]


def is_geometry(entity):
    return entity.dxftype() in DXF_CONVERTERS


def load(source, workdir, cache_dir=None, timeout=cadcraft_tool.DEFAULT_TIMEOUT):
    """The drawing in 'source' (DWG or DXF) as an ezdxf document, and what CADCraft warned about."""
    if not source.lower().endswith(".dxf"):
        dxf = os.path.join(workdir, os.path.splitext(os.path.basename(source))[0] + ".dxf")
        warnings = cadcraft_tool.convert(source, dxf, cache_dir=cache_dir, timeout=timeout)
        return ezdxf.readfile(dxf), warnings
    try:
        return ezdxf.readfile(source), []
    except Exception as e:
        # Not every DXF a tool writes is one ezdxf accepts; CADCraft reads more
        # of them and writes back what it read.
        clean = os.path.join(workdir, "normalized.dxf")
        warnings = cadcraft_tool.convert(source, clean, cache_dir=cache_dir, timeout=timeout)
        return (
            ezdxf.readfile(clean),
            ["read through CADCraft, since ezdxf could not read it as it is (%s)" % e] + warnings,
        )


def millimetres_per_unit(document, units):
    """How much to scale the drawing by, and what to call the unit it is in."""
    units = str(units or "auto").strip().lower()
    if units != "auto":
        if units not in MILLIMETRES_PER_UNIT:
            raise ValueError(
                "unknown 'units' %r: expected auto or one of %s" % (units, ", ".join(MILLIMETRES_PER_UNIT))
            )
        return MILLIMETRES_PER_UNIT[units], units
    code = int(document.header.get("$INSUNITS", 0) or 0)
    if code not in MILLIMETRES_PER_INSUNITS:
        raise ValueError("the drawing's $INSUNITS (%d) is not a length PartCAD can scale; set 'units'" % code)
    return MILLIMETRES_PER_INSUNITS[code], (ezdxf.units.decode(code) if code else "unitless")


def count_dropped(entities, into=None):
    """How many of 'entities' of each kind are not geometry (block references aside)."""
    dropped = into if into is not None else {}
    for entity in entities:
        kind = entity.dxftype()
        if kind in DXF_CONVERTERS or kind == "INSERT":
            continue
        category = NOT_GEOMETRY.get(kind, "other")
        dropped[category] = dropped.get(category, 0) + 1
    return dropped


def references(insert):
    """The block references one INSERT stands for: itself, or each cell of a MINSERT's grid."""
    if insert.mcount > 1:
        return list(insert.multi_insert())
    return [insert]


def explode_blocks(layout):
    """Replace every block reference in 'layout' with the entities it draws, layer 0 taking the reference's layer."""
    for _ in range(MAX_BLOCK_DEPTH):
        inserts = list(layout.query("INSERT"))
        if not inserts:
            return
        for insert in inserts:
            layer = insert.dxf.layer
            for entity in insert.explode():
                if entity.dxf.layer == "0":
                    entity.dxf.layer = layer
    raise ValueError(
        "blocks are placed inside blocks more than %d deep (do they refer to each other?)" % MAX_BLOCK_DEPTH
    )


def flattened(insert, depth=0):
    """What one block reference draws, as virtual entities in the coordinates it is placed in."""
    if depth > MAX_BLOCK_DEPTH:
        raise ValueError("blocks are placed inside blocks more than %d deep" % MAX_BLOCK_DEPTH)
    for entity in insert.virtual_entities():
        if entity.dxftype() == "INSERT":
            yield from flattened(entity, depth + 1)
        else:
            yield entity


class Layers:
    """Which layers of a drawing are read: 'includeLayers' or 'excludeLayers', matched without regard to case."""

    def __init__(self, include=None, exclude=None):
        self.include = {name.lower() for name in as_list(include)}
        self.exclude = {name.lower() for name in as_list(exclude)}
        if self.include and self.exclude:
            raise ValueError("you may specify either 'includeLayers' or 'excludeLayers' but not both")

    def __contains__(self, layer):
        layer = str(layer).lower()
        if self.include:
            return layer in self.include
        return layer not in self.exclude


def wires(entities, tolerance, scale):
    """The wires the geometry among 'entities' draws, joined end to end, in millimetres.

    An arc, a circle or a polyline is stated in a coordinate system of its own,
    and one that was mirrored -- by AutoCAD's MIRROR, or by a block reference
    with a negative scale -- states it upside down (extrusion (0, 0, -1)), its X
    running the other way. CadQuery's converters read every one as if it were
    the right way up, which puts a mirrored circle on the far side of the Y axis.
    So each is turned the right way up first, which describes the same curve in
    the drawing's own coordinates.
    """
    geometry = [e for e in entities if is_geometry(e)]
    for entity in geometry:
        if entity.dxf.hasattr("extrusion") and entity.dxf.extrusion.z < 0:
            upright(entity)
    found = _dxf_convert(geometry, tolerance)
    return [w if scale == 1.0 else w.scale(scale) for w in found]


def faces(found):
    """The faces closed wires bound, holes included; raises if a wire does not close."""
    return [Face.makeFromWires(wire_set[0], wire_set[1:]) for wire_set in sortWiresByBuildOrder(found)]


def profile(found):
    """The faces the wires bound or, when they do not close, the wires themselves.

    Returns (shape, closed, warning). The fallback is PartCAD's own: a drawing of
    open lines -- a sheet metal blank's bend lines -- is still a drawing, and is
    kept as the wires it draws rather than refused.
    """
    if not found:
        return None, False, None
    try:
        return Compound.makeCompound(faces(found)), True, None
    except Exception as e:
        return Compound.makeCompound(found), False, "no face could be built (%s); kept the wires it draws instead" % e


def extrude(shape, thickness):
    """Every face of 'shape' extruded along +Z by 'thickness' millimetres."""
    solids = [Solid.extrudeLinear(face, Vector(0, 0, thickness)) for face in shape.Faces()]
    if not solids:
        raise ValueError("there is no closed outline to extrude")
    return solids[0] if len(solids) == 1 else Compound.makeCompound(solids)


def placement(matrix, tolerance=1e-9):
    """A block reference's transform as (translation, axis, degrees, scale), or None.

    None when it is not a rotation, a translation and one uniform scale -- a
    mirrored reference, or one scaled differently along X and Y -- which a
    placement cannot say. The Z scale is not asked about: the geometry is flat.
    """
    ux, uy, uz = matrix.ux, matrix.uy, matrix.uz
    sx, sy = ux.magnitude, uy.magnitude
    if sx <= tolerance or abs(sx - sy) > max(tolerance, 1e-6 * sx):
        return None
    x, y = ux / sx, uy / sy
    z = uz.normalize() if uz.magnitude > tolerance else x.cross(y)
    if abs(x.dot(y)) > 1e-6 or x.cross(y).dot(z) <= 0:
        return None
    # The rotation's columns are where X, Y and Z go; its axis and angle by
    # Rodrigues' formula, with the half turn (where that formula divides by
    # nothing) read off the diagonal instead.
    r = [[x.x, y.x, z.x], [x.y, y.y, z.y], [x.z, y.z, z.z]]
    cos = max(-1.0, min(1.0, (r[0][0] + r[1][1] + r[2][2] - 1.0) / 2.0))
    angle = math.acos(cos)
    if angle < 1e-9:
        axis = (0.0, 0.0, 1.0)
    elif math.pi - angle < 1e-6:
        diagonal = [math.sqrt(max(0.0, (r[i][i] + 1.0) / 2.0)) for i in range(3)]
        i = diagonal.index(max(diagonal))
        axis = [0.0, 0.0, 0.0]
        axis[i] = diagonal[i]
        for j in range(3):
            if j != i:
                axis[j] = r[i][j] / (2.0 * diagonal[i])
        axis = tuple(axis)
    else:
        s = 2.0 * math.sin(angle)
        axis = ((r[2][1] - r[1][2]) / s, (r[0][2] - r[2][0]) / s, (r[1][0] - r[0][1]) / s)
    origin = matrix.origin
    return (origin.x, origin.y, origin.z), axis, math.degrees(angle), sx


def safe_name(name):
    """What something is called, made a valid PartCAD object name."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(name)).strip("_") or "item"


def node_name(name, taken):
    """A PartCAD object name: what something is called, made safe and unique among 'taken'."""
    safe = safe_name(name)
    candidate, n = safe, 2
    while candidate.lower() in taken:
        candidate, n = "%s_%d" % (safe, n), n + 1
    taken.add(candidate.lower())
    return candidate


def wrapped(shape):
    """The OCCT shape a CadQuery object carries, which is what PartCAD is handed."""
    return shape.wrapped if isinstance(shape, Shape) else shape
