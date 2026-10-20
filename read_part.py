#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""The partTypes this package declares: an AutoCAD drawing, DWG or DXF, as a part.

The drawing's outlines are the part: the layers it reads are read as one
drawing, so an outline on one layer and the holes in it on another make one face
with holes, and block references are exploded where they are. With 'thickness'
set the faces are extruded along +Z by that many millimetres -- the part a
drawing of a plate stands for. Without it the part is the faces themselves, or
the wires for a drawing that does not close.

Run by PartCAD's partType wrapper with 'request["path"]' set to the drawing and
'request["cache_dir"]' to where 'cadcraft-cli' is kept. Parameters, from the
part's 'parameters:' or, failing that, its own fields:

    thickness       extrusion in millimetres; 0 (the default) keeps the part flat
    includeLayers   the layers to read: a list, or a comma-separated string
    excludeLayers   the layers to skip; not together with 'includeLayers'
    units           'auto' (the drawing's $INSUNITS), or mm, cm, m, in, ft, none
    joinTolerance   how far apart two ends may be and still join (default 1e-6)

They are not 'include', 'exclude' and 'tolerance' because PartCAD already means
something by those on an object.
"""

import os
import sys
import tempfile

# Siblings are imported by bare name, and the sandbox sets PYTHONSAFEPATH.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Raise with any change to what this produces (see PartCAD's 'code_version').
CACHE_VERSION = 1


def parameter(request, name, default=None):
    parameters = request.get("parameters") or {}
    if parameters.get(name) is not None:
        return parameters[name]
    value = (request.get("config") or {}).get(name)
    return default if value is None else value


def build(request):
    import cadcraft_drawing as drawing

    source = request["path"]
    thickness = float(parameter(request, "thickness", 0) or 0)
    layers = drawing.Layers(parameter(request, "includeLayers"), parameter(request, "excludeLayers"))
    with tempfile.TemporaryDirectory(prefix="partcad-cadcraft-") as workdir:
        document, warnings = drawing.load(source, workdir, cache_dir=request.get("cache_dir"))
        scale, _unit = drawing.millimetres_per_unit(document, parameter(request, "units", "auto"))
        modelspace = document.modelspace()
        drawing.explode_blocks(modelspace)
        selected = [e for e in modelspace if e.dxf.layer in layers]
        found = drawing.wires(selected, float(parameter(request, "joinTolerance", 1e-6)), scale)
    for line in warnings:
        sys.stderr.write("cadcraft: %s\n" % line)
    name = os.path.basename(source)
    if not found:
        raise ValueError("%s: the layers read draw no lines, arcs, circles, ellipses, splines or polylines" % name)
    shape, closed, warning = drawing.profile(found)
    if warning:
        sys.stderr.write("%s: %s\n" % (name, warning))
    if thickness:
        if not closed:
            raise ValueError("%s: %s, so there is nothing to extrude" % (name, warning))
        shape = drawing.extrude(shape, thickness)
    return drawing.wrapped(shape)


if __name__ == "__partcad_part__":
    try:
        output = {"shape": build(request)}  # noqa: F821 - injected by the partType wrapper
    except Exception as e:
        output = {"exception": str(e)}
