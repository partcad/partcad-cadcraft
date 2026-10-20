#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Read an AutoCAD drawing, DWG or DXF, as a PartCAD assembly (the 'import:' entries).

An assembly is parts placed relative to each other, and a drawing has the same
structure in its blocks: a block is a piece of geometry defined once, and every
block reference (INSERT) places a copy of it, moved, rotated and scaled. So:

* **each block is a part**, named after the block -- its geometry, in its own
  coordinates, as faces (or extruded by 'thickness');
* **each block reference is a placed instance** of that part, at the position
  and rotation the reference gives it. A drawing with twelve references to
  'BOLT' is an assembly holding the part 'BOLT' twelve times;
* **a block placed inside a block** makes the outer one a sub-assembly: its own
  geometry, plus its references placed within it;
* **geometry outside any block** is one part per layer, named after the layer,
  at the drawing's own coordinates.

What a placement cannot say is made part of the geometry instead: a uniformly
scaled reference is a part of its own for that scale ('BOLT_scaled_2'), and a
mirrored one, or one scaled differently along X and Y, is a part of its own
with its whole transform in it. Nothing is lost; only what can be shared is.

Run by PartCAD's import wrapper with 'request' -- 'source_file',
'output_folder', 'object_name', 'cache_dir' and this reader's parameters -- and
'path', a directory it may write into. Each part is written as a BREP into the
output folder and named in the tree; PartCAD's own BREP part type reads it.
"""

import os
import sys
import tempfile

# Siblings are imported by bare name, and the sandbox sets PYTHONSAFEPATH.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Raise with any change to what this produces (see PartCAD's 'code_version').
CACHE_VERSION = 1


def _scale_label(scale):
    text = ("%.6g" % scale).replace(".", "_").replace("-", "m")
    return text


class Reader:
    def __init__(self, document, request, drawing):
        self.drawing = drawing
        self.document = document
        self.output_folder = request["output_folder"]
        self.tolerance = float(request.get("joinTolerance") or 1e-6)
        self.thickness = float(request.get("thickness") or 0)
        self.unit, self.unit_name = drawing.millimetres_per_unit(document, request.get("units") or "auto")
        self.layers = drawing.Layers(request.get("includeLayers"), request.get("excludeLayers"))
        self.names = set()
        self.files = {}  # (block, scale) -> (name, file), or None for a block that draws nothing
        self.warnings = []
        self.dropped = {}
        self.counted = set()
        self.instances = 0

    def write(self, name, entities, scale):
        """Write what 'entities' draw, scaled to millimetres, as a BREP; None when they draw nothing."""
        found = self.drawing.wires(entities, self.tolerance, scale)
        shape, closed, warning = self.drawing.profile(found)
        if shape is None:
            return None
        if warning:
            self.warnings.append("%s: %s" % (name, warning))
        if self.thickness and closed:
            shape = self.drawing.extrude(shape, self.thickness)
        elif self.thickness:
            self.warnings.append("%s has no closed outline, so it is not extruded" % name)
        from OCP.BRepTools import BRepTools

        path = os.path.join(self.output_folder, name + ".brep")
        BRepTools.Write_s(self.drawing.wrapped(shape), path)
        return path

    def block_part(self, block_name, scale):
        """The part one block's own geometry is at one scale: (name, file), or None if it draws nothing."""
        key = (block_name.lower(), round(scale, 9))
        if key not in self.files:
            block = self.document.blocks.get(block_name)
            if block_name.lower() not in self.counted:
                self.counted.add(block_name.lower())
                self.drawing.count_dropped(block, into=self.dropped)
            base = (
                block_name
                if abs(scale - self.unit) < 1e-12
                else "%s_scaled_%s" % (block_name, _scale_label(scale / self.unit))
            )
            name = self.drawing.node_name(base, self.names)
            path = self.write(name, list(block), scale) if block is not None else None
            self.files[key] = (name, path) if path else None
        return self.files[key]

    def instance(self, reference, outer, depth=0):
        """The tree node one block reference becomes, placed in what holds it; None if it draws nothing.

        'outer' is the scale what holds it was placed at, in millimetres per
        drawing unit -- the drawing's own unit at the top, times every enclosing
        reference's scale below it.
        """
        if depth > self.drawing.MAX_BLOCK_DEPTH:
            raise ValueError("blocks are placed inside blocks more than %d deep" % self.drawing.MAX_BLOCK_DEPTH)
        self.instances += 1
        block_name = reference.dxf.name
        block = self.document.blocks.get(block_name)
        if block is None:
            self.warnings.append("a reference names the block '%s', which the drawing does not define" % block_name)
            return None
        rigid = self.drawing.placement(reference.matrix44())
        if rigid is None:
            # Mirrored, or scaled unevenly: the whole transform goes into a
            # part of this reference's own, placed where its holder is.
            name = self.drawing.node_name("%s_transformed" % block_name, self.names)
            path = self.write(name, list(self.drawing.flattened(reference)), outer)
            if path is None:
                return None
            return {"type": "part", "name": name, "part_file": path, "part_type": "brep"}

        translation, axis, degrees, scale = rigid
        location = [[v * outer for v in translation], list(axis), degrees]
        inner = outer * scale
        own = self.block_part(block_name, inner)
        nested = [e for e in block if e.dxftype() == "INSERT"]
        if not nested:
            if own is None:
                self.dropped["block"] = self.dropped.get("block", 0) + 1
                return None
            return {"type": "part", "name": own[0], "part_file": own[1], "part_type": "brep", "location": location}

        links = []
        if own is not None:
            links.append({"type": "part", "name": own[0], "part_file": own[1], "part_type": "brep"})
        for insert in nested:
            for cell in self.drawing.references(insert):
                child = self.instance(cell, inner, depth + 1)
                if child is not None:
                    links.append(child)
        if not links:
            self.dropped["block"] = self.dropped.get("block", 0) + 1
            return None
        return {"type": "assembly", "name": self.drawing.safe_name(block_name), "location": location, "links": links}

    def read(self, object_name):
        modelspace = self.document.modelspace()
        links = []
        # Each block reference in modelspace, in the order the drawing has them.
        for insert in modelspace.query("INSERT"):
            if insert.dxf.layer not in self.layers:
                continue
            for cell in self.drawing.references(insert):
                node = self.instance(cell, self.unit)
                if node is not None:
                    links.append(node)
        # Then what is drawn outside any block, a part per layer.
        loose = [e for e in modelspace if e.dxftype() != "INSERT"]
        self.drawing.count_dropped(loose, into=self.dropped)
        by_layer = {}
        for entity in loose:
            if entity.dxf.layer in self.layers:
                by_layer.setdefault(entity.dxf.layer, []).append(entity)
        empty = 0
        for layer, entities in by_layer.items():
            if not any(self.drawing.is_geometry(e) for e in entities):
                empty += 1
                continue
            name = self.drawing.node_name(layer, self.names)
            path = self.write(name, entities, self.unit)
            if path is None:
                empty += 1
                continue
            links.append({"type": "part", "name": name, "part_file": path, "part_type": "brep"})
        if empty:
            self.dropped["layer"] = self.dropped.get("layer", 0) + empty
        return {"type": "assembly", "name": object_name, "links": links}


def process(path, request):
    import cadcraft_drawing as drawing

    source = request["source_file"]
    os.makedirs(request["output_folder"], exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="partcad-cadcraft-") as workdir:
        document, converted = drawing.load(
            source,
            workdir,
            cache_dir=request.get("cache_dir"),
            timeout=float(request.get("cadcraftTimeout") or 600),
        )
    reader = Reader(document, request, drawing)
    name = os.path.basename(source)
    root = reader.read(request.get("object_name") or name)
    if not root["links"]:
        raise ValueError(
            "%s: nothing it draws is selected (layers: %s)"
            % (name, ", ".join(sorted({e.dxf.layer for e in document.modelspace()})) or "none")
        )
    blocks = {block for (block, _scale), part in reader.files.items() if part is not None}
    return {
        "root": root,
        "warnings": ["CADCraft: %s" % line for line in converted] + reader.warnings,
        "dropped": reader.dropped,
        "model_name": name,
        "info": {
            "Drawing": name,
            "DrawingUnits": reader.unit_name,
            "Blocks": len(blocks),
            "BlockReferences": reader.instances,
        },
    }
