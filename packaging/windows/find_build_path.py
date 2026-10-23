"""Fail when any staged file contains a path of the build machine (checkout, stage, profile).

    python find_build_path.py <stage dir> <path that must not appear> [...]

The path of the developer's checkout says who built the package and where; it has no
business in an installer. Bytecode is the usual carrier (compileall records the source
path), so build.ps1 strips it and runs this check afterwards. Exit 1 lists the files.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def needles(paths: list[str]) -> list[bytes]:
    out: list[bytes] = []
    for p in paths:
        for variant in {p, p.replace("\\", "/"), p.lower(), p.lower().replace("\\", "/")}:
            out.append(variant.encode("utf-8"))
            out.append(variant.encode("utf-16-le"))
    return out


def main(argv: list[str]) -> int:
    stage, forbidden = Path(argv[1]), needles(argv[2:])
    found: list[str] = []
    for root, _dirs, files in os.walk(stage):
        for name in files:
            path = Path(root, name)
            data = path.read_bytes()
            lowered = data.lower()
            if any(n in data or n in lowered for n in forbidden):
                found.append(str(path.relative_to(stage)))
    if found:
        print(f"{len(found)} staged file(s) contain the build path, e.g.:", *found[:10], sep="\n  ")
        return 1
    print("no staged file contains the build path")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
