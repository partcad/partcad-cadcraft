# CADCraft for PartCAD

A [PartCAD](https://github.com/partcad/partcad) plugin package for [CADCraft](https://github.com/storytold/cadcraft),
the open-source AutoCAD-style drafting application written in Rust. It reads and writes AutoCAD's two drawing
formats, DWG and DXF:

| What | Declared as | Section |
|---|---|---|
| A part read from a drawing: its outlines, extruded | `type: cadcraft:dwg` (or `cadcraft:dxf`) under `parts:` | `partTypes:` |
| An assembly read from a drawing: each block a part, each block reference an instance | `type: cadcraft:dwg` (or `cadcraft:dxf`) under `assemblies:` | `import:` |
| A drawing of any object, as DWG or DXF | `pc render -t cadcraft:dwg` (or `cadcraft:dxf`) | `render:` |
| CADCraft itself, to view and edit a drawing in | `pc ide open --with cadcraft`, or *Open in > CADCraft* in the PartCAD extension for VS Code | `open:` |

`cadcraft:` is the name the workspace imports this package under; use whatever name the dependency is given.

It needs a PartCAD whose partTypes can read a file -- the `extension:` of a `partType` -- which is what hands a
script the part's file and this package's cache directory; and, for the assemblies, one that hands an `import:`
reader a binary file as it is.

## Using it

```yaml
dependencies:
  cadcraft:
    type: git
    url: https://github.com/partcad/partcad-cadcraft
    track: main

parts:
  plate:
    type: cadcraft:dwg            # reads plate.dwg
    parameters:
      thickness:                  # extruded 3 mm
        type: float
        default: 3
      excludeLayers:
        type: string
        default: BENDS,NOTES
  gasket:
    type: cadcraft:dxf
    path: vendor/gasket.dxf       # relative to this package, as for any file-backed part

assemblies:
  enclosure:
    type: cadcraft:dwg            # reads enclosure.dwg
    thickness: 2
```

```shell
pc render -t cadcraft:dwg :plate     # a plan view of the plate, as a DWG
pc render -t cadcraft:dxf -a enclosure
```

Each part or assembly is one file, declared exactly like a STEP part: `path` relative to the package declaring it,
`<name>.<extension>` when it is left out, and `fileFrom`/`fileUrl` to fetch it. The file is what its cache entry
depends on, so editing the drawing rebuilds it.

### A part from a drawing

The drawing's outlines are the part. The layers it reads are read as one drawing, so an outline on one layer and
the holes in it on another make one face with holes; block references are exploded where they are. With
`thickness` the faces are extruded along +Z -- the part a drawing of a plate stands for. Without it the part is
the faces themselves, or the wires for a drawing that does not close (a sheet metal blank's bend lines).

| Parameter | Default | |
|---|---|---|
| `thickness` | `0` | extrusion in millimetres; `0` keeps the part flat |
| `includeLayers` | all | the layers to read: a list, or a comma-separated string |
| `excludeLayers` | none | the layers to skip; not together with `includeLayers` |
| `units` | `auto` | `auto` reads the drawing's `$INSUNITS` (a drawing in inches is scaled by 25.4); or `mm`, `cm`, `m`, `in`, `ft`, `none` |
| `joinTolerance` | `1e-6` | how far apart two ends may be and still join into one outline |

They are not `include`, `exclude` and `tolerance`, because PartCAD already means something by those on an object.

`cadcraft:dxf` is an alternative to a PartCAD `dxf` sketch extruded, which stays as it is. Beside what is above, it
reads a DXF ezdxf cannot -- an old R12, an oddly written binary one -- by having CADCraft read it first.

### An assembly from a drawing

In PartCAD an assembly is parts placed relative to each other. A drawing has the same structure, in its
**blocks**: a block is a piece of geometry defined once, and every **block reference** (an `INSERT`) places a copy
of it -- moved, rotated, scaled. So reading a drawing as an assembly means:

* **each block is a part**, named after the block: its geometry in its own coordinates, as faces, extruded by
  `thickness`;
* **each block reference is a placed instance** of that part, at the position and rotation it gives. A drawing
  with twelve references to `BOLT` is an assembly holding the part `BOLT` twelve times, and its bill of materials
  says so;
* **a block placed inside a block** makes the outer one a sub-assembly: its own geometry, plus its references
  placed within it;
* **what is drawn outside any block** is one part per layer, named after the layer, where the drawing has it.

What a placement cannot say goes into the geometry instead: a reference at twice the size is a part of its own
for that scale (`BOLT_scaled_2`), and a mirrored one, or one stretched differently along X and Y, is a part of its
own with its whole transform in it (`BOLT_transformed`). Nothing is lost; only what can be shared is.

It takes the parameters above, set on the assembly itself rather than under `parameters:` -- the layers filter
the block references by the layer they are on, and the loose geometry by its own -- plus `cadcraftTimeout`
(seconds, default 600). What the drawing says that is not geometry -- text, dimensions, hatches, layers with
nothing on them -- is counted, and `pc info` shows it under `Dropped`.

### A drawing of an object

```yaml
render:
  dwg:
    reproducible: true            # the same bytes for the same drawing, for DWGs kept in git
    hidden_lines: true            # hidden edges, dashed, on a layer 'Hidden'
    dwgVersion: 2013
  dxf:
    dxfVersion: 2000
    binary: true
```

These are **renders, not exports**, and the difference is the one PartCAD draws everywhere. An *export* is
geometry another CAD tool can go on working with -- STEP, BREP, STL. A *render* is an output file: a picture, a
drawing. A DWG of a part is a drawing of it -- its edges seen from one direction, flat -- and nothing in it is the
solid; CADCraft cannot write a solid (an ACIS `3DSOLID`) into either format in any case. So both file types are
declared under `render:`, which promises an output file and nothing more, and this package declares no `export:`.
(`pc export -t cadcraft:dwg` writes the same file, because a section follows the declaration, not the command.)

* A **sketch** is drawn as the edges it is made of, untouched: it is already flat, so nothing is projected, and a
  sketch read from a drawing comes back out with its arcs, circles and coordinates as they were.
* A **part or an assembly** is projected through OCCT's exact hidden-line algorithm, from `viewport_origin`
  (`[0, 0, 100]`, a plan view) with `viewport_up` (`[0, 1, 0]`); `pc render --view` changes both.

Lines, arcs, circles, ellipses and splines are written as what they are -- PartCAD's built-in `dxf` render goes
through its SVG projection and flattens every curve into straight segments. Then CADCraft saves the drawing:

| Parameter | Default | |
|---|---|---|
| `dwgVersion` / `dxfVersion` | `2018` | 2000, 2004, 2007, 2010, 2013 or 2018 |
| `binary` | `false` | a binary DXF (`cadcraft:dxf` only) |
| `hidden_lines` | `false` | hidden edges too, dashed, on their own layer |
| `line_weight` | none | in millimetres |
| `reproducible` | `false` | fixed creation time and GUIDs instead of the clock's |

### Opening a drawing in CADCraft

`pc ide open --with cadcraft <file>` opens a DWG or a DXF in CADCraft, and in VS Code *Open in > CADCraft* is on
the objects it can open as they are: a DXF sketch, and a part or an assembly of a DWG or DXF type. It finds an
installed CADCraft -- the `PATH`, `CADCraft.app`, `CADCraft` under the Windows program directories, the
`ai.storyteller.cadcraft` Flatpak -- and otherwise runs it in this package's image, pulled on demand, with the
display forwarded into it.

What is edited there is the drawing itself, and CADCraft saves over it. CADCraft leaves out what it does not
model when it saves (see its `docs/file-format-parity.md`), so keep the original of a drawing that came from
elsewhere.

## Where CADCraft comes from

Everything here runs CADCraft's command line tool, `cadcraft-cli`. Whichever comes first is used:

1. **`$CADCRAFT_CLI`**, for a machine that keeps one of its own somewhere unusual.
2. **A `cadcraft-cli` on the `PATH`.** A machine that installed CADCraft, or this package's image.
3. **This package's image**, `ghcr.io/partcad/partcad-cadcraft:<tag>`, named as the package's `dockerImage`.
   PartCAD runs the scripts in it where its `docker` sandbox is in use and the image can be pulled, and falls back to
   its own sandbox, with a note, where it cannot.
4. **A download on first use.** The release pinned in [`cadcraft_tool.py`](cadcraft_tool.py), verified against its
   published SHA-256 before it is unpacked, kept in this package's cache directory -- `.cache/` beside this file,
   which in a workspace is PartCAD's cached copy of this package -- under its version and platform, and reused
   from then on. Where that directory cannot be written, the user's cache directory is used.

CADCraft publishes a command line tool for Linux (x86_64, aarch64, riscv64), macOS (universal), Windows (x64, arm64,
x86) and FreeBSD (x86_64), so the download works on every operating system PartCAD runs on, and the image covers
Linux on x86_64 and aarch64 wherever Docker runs.

## The image

[`Dockerfile`](Dockerfile) builds it `FROM` PartCAD's Python sandbox image, adding CADCraft -- the command line tool
and the desktop application -- and the libraries the application opens at run time;
[`verify_image.py`](verify_image.py) runs at build time and refuses an image whose CADCraft cannot save and read back
a DWG. [`.github/workflows/image.yml`](.github/workflows/image.yml) builds it for `amd64` and `arm64` on every pull
request and publishes it from `main`, tagged with a hash of what it is built from ([`image-tag.sh`](image-tag.sh))
rather than a version. The tag is immutable, and `partcad.yaml` pins it twice -- as the package's `dockerImage` and
as the `open:` entry's container -- which the workflow and a test both check.

After changing the `Dockerfile` or `verify_image.py`, run `./image-tag.sh` and put what it prints into both pins in
the same commit.

## Tests

[`tests/`](tests) is a PartCAD package of its own: a workspace that imports this plugin through a `local` dependency
and declares a part and an assembly from each format, with the drawings `make_samples.py` wrote. The empty
`.pcignore` in it keeps it out of this package's tree, so it is never listed as the plugin's subpackage wherever the
plugin is used.

```shell
pip install -r requirements-test.txt
pip install --force-reinstall --no-deps cadquery-ocp==7.9.3.1.1
pytest                 # everything; the first part built downloads CADCraft
pytest -m "not slow"   # without building anything in PartCAD
```

What needs nothing but the standard library (the downloader) always runs; the geometry needs CadQuery and skips
without it; what goes through PartCAD needs PartCAD and skips without it, or with one too old.
`CADCRAFT_NETWORK_TESTS=1` also checks the pinned checksums against the release itself.

## Updating CADCraft

Change `VERSION` and every checksum in `cadcraft_tool.py` (from the release's `SHA256SUMS.txt`), the version and
both Linux checksums in the `Dockerfile` (a test holds the two to each other), then run `./image-tag.sh` and update
both pins in `partcad.yaml`. Raise `CACHE_VERSION` in `read_part.py`, `read_assembly.py` and `render_drawing.py`,
and run `python tests/make_samples.py` to write the sample DWGs again.

## Licences

This package is Apache-2.0. CADCraft is MIT OR Apache-2.0 and reads DWG with the
[acadrust](https://docs.rs/acadrust) library (MPL-2.0); it is downloaded from its own releases rather than
redistributed here, and the image carries a copy of it, licences included.
