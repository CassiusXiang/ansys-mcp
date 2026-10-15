"""Convert a STEP file to Parasolid text (.x_t) via the Ansys Geometry Service.

Usage: python step_to_parasolid.py <input.step> <output.x_t>
Runs in its own venv (.venv-geom) because ansys-geometry-core needs a newer
protobuf than PyMAPDL tolerates.
"""
import sys
from pathlib import Path

from ansys.geometry.core import launch_modeler
from ansys.geometry.core.designer.design import DesignFileFormat


def main() -> int:
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    modeler = launch_modeler(timeout=240)
    try:
        design = modeler.open_file(src)
        bodies = list(design.bodies)
        for comp in design.components:
            bodies.extend(comp.bodies)
        print(f"opened {src.name}: {len(bodies)} bodies: {[b.name for b in bodies][:20]}")
        design.download(dst, DesignFileFormat.PARASOLID_TEXT)
        print(f"wrote {dst.name}: {dst.stat().st_size} bytes")
        return 0
    finally:
        modeler.close()


if __name__ == "__main__":
    sys.exit(main())
