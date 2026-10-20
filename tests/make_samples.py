#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Write the drawings the tests read: plate.dxf and kit.dxf, and a DWG of each.

    python tests/make_samples.py

Needs ezdxf; the DWGs are saved by CADCraft, found or downloaded the way the
plugin finds it. Re-run after changing a drawing here, and commit what it wrote.

plate -- a 4 x 2 inch plate: what a part is read from.

    OUTLINE   the plate's outline, a closed polyline
    HOLES     two references to the block BOLT, whose circle is on layer 0
    BENDS     a bend line, open
    NOTES     a note

kit -- millimetres: what an assembly is read from, one of each kind of block
reference the reader tells apart.

    BRACKET   a 40 x 30 plate with a hole, referenced twice -- once as it is,
              once turned a quarter turn -- and once mirrored
    PIN       a circle of radius 3, referenced inside FRAME twice and once on
              its own at twice its size
    FRAME     a 100 x 20 bar holding two PIN references: a sub-assembly
    BASE      a 200 x 100 outline drawn outside any block
    NOTES     a note
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402

import cadcraft_tool  # noqa: E402


def stable(document, path):
    """Save 'document' so that saving it again writes the same bytes."""
    ezdxf.options.write_fixed_meta_data_for_testing = True
    for dxftype in sorted(document.entitydb.dxf_types_in_use()):
        document.classes.add_class(dxftype)
    document.saveas(path)


def make_plate(path):
    document = ezdxf.new("R2010", units=ezdxf.units.IN)
    for layer in ("OUTLINE", "HOLES", "BENDS", "NOTES"):
        document.layers.add(layer)
    bolt = document.blocks.new("BOLT")
    bolt.add_circle((0, 0), 0.25, dxfattribs={"layer": "0"})
    modelspace = document.modelspace()
    modelspace.add_lwpolyline([(0, 0), (4, 0), (4, 2), (0, 2)], close=True, dxfattribs={"layer": "OUTLINE"})
    modelspace.add_blockref("BOLT", (1, 1), dxfattribs={"layer": "HOLES"})
    modelspace.add_blockref("BOLT", (3, 1), dxfattribs={"layer": "HOLES"})
    modelspace.add_line((2, 0), (2, 2), dxfattribs={"layer": "BENDS"})
    modelspace.add_text("4 x 2 in plate", dxfattribs={"layer": "NOTES", "height": 0.1}).set_placement((0.1, 1.8))
    stable(document, path)


def make_kit(path):
    document = ezdxf.new("R2010", units=ezdxf.units.MM)
    for layer in ("PARTS", "BASE", "NOTES"):
        document.layers.add(layer)
    bracket = document.blocks.new("BRACKET")
    bracket.add_lwpolyline([(0, 0), (40, 0), (40, 30), (0, 30)], close=True)
    bracket.add_circle((10, 10), 4)
    pin = document.blocks.new("PIN")
    pin.add_circle((0, 0), 3)
    frame = document.blocks.new("FRAME")
    frame.add_lwpolyline([(0, 0), (100, 0), (100, 20), (0, 20)], close=True)
    frame.add_blockref("PIN", (20, 10))
    frame.add_blockref("PIN", (80, 10))

    modelspace = document.modelspace()
    modelspace.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True, dxfattribs={"layer": "BASE"})
    modelspace.add_blockref("BRACKET", (10, 10), dxfattribs={"layer": "PARTS"})
    modelspace.add_blockref("BRACKET", (150, 10), dxfattribs={"layer": "PARTS", "rotation": 90})
    modelspace.add_blockref("FRAME", (50, 60), dxfattribs={"layer": "PARTS"})
    modelspace.add_blockref("PIN", (180, 80), dxfattribs={"layer": "PARTS", "xscale": 2, "yscale": 2, "zscale": 2})
    modelspace.add_blockref("BRACKET", (100, 10), dxfattribs={"layer": "PARTS", "xscale": -1})
    modelspace.add_text("kit", dxfattribs={"layer": "NOTES", "height": 5}).set_placement((5, 95))
    stable(document, path)


if __name__ == "__main__":
    for name, make in (("plate", make_plate), ("kit", make_kit)):
        dxf = os.path.join(HERE, name + ".dxf")
        make(dxf)
        cadcraft_tool.save_as(dxf, os.path.join(HERE, name + ".dwg"), version="2018")
        print("wrote %s.dxf and %s.dwg" % (name, name))
