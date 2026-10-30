"""Add PyPI's SHA-256 hashes to packaging/windows/requirements.txt (audit D12).

build.ps1 installs the runtime with ``pip install --require-hashes``: every pinned file must
match a hash listed here, so a replaced or tampered upload on PyPI fails the build instead of
being bundled. For each ``name==version`` this lists the files a CPython 3.12 x64 Windows build
can install (``win_amd64`` and pure-Python wheels; the source archive only for the packages
built from source), from PyPI's JSON API.

    python packaging/windows/hash_requirements.py            # rewrite requirements.txt
    python packaging/windows/hash_requirements.py --check    # exit 1 if a pin has no hash

Run it after regenerating the pins (docs/windows-installer.md). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

REQUIREMENTS = Path(__file__).with_name("requirements.txt")
# Built from their source archive (build.ps1 --no-binary): proxy_tools publishes no wheel.
SOURCE_BUILT = {"proxy-tools"}
_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\;]+)")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def installable(filename: str, name: str) -> bool:
    """Whether a CPython 3.12 x64 Windows install could pick this file."""
    if filename.endswith((".tar.gz", ".zip")):
        return norm(name) in SOURCE_BUILT
    if not filename.endswith(".whl"):
        return False
    python, abi, platform = filename[:-4].split("-")[-3:]
    if platform != "any" and "win_amd64" not in platform:
        return False
    return any(tag in ("py3", "cp312") or tag.startswith("py3") for tag in python.split(".")) or abi == "abi3"


def pypi_hashes(name: str, version: str) -> list[str]:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - a fixed https URL
        files = json.load(resp)["urls"]
    return sorted(f["digests"]["sha256"] for f in files if installable(f["filename"], name))


def pins(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for line in text.splitlines() if (m := _PIN.match(line))]


def render(text: str) -> str:
    header = [line for line in text.splitlines() if line.startswith("#")]
    out = header[:]
    for name, version in pins(text):
        hashes = pypi_hashes(name, version)
        if not hashes:
            raise SystemExit(f"{name}=={version}: no installable file on PyPI")
        out.append(f"{name}=={version} \\")
        out.extend(f"    --hash=sha256:{h}" + (" \\" if i < len(hashes) - 1 else "") for i, h in enumerate(hashes))
    return "\n".join(out) + "\n"


def unhashed(text: str) -> list[str]:
    """Pins not followed by at least one ``--hash`` line."""
    lines = text.splitlines()
    missing = []
    for i, line in enumerate(lines):
        m = _PIN.match(line)
        if m and not (line.rstrip().endswith("\\") and i + 1 < len(lines) and "--hash=sha256:" in lines[i + 1]):
            missing.append(m.group(1))
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="only check that every pin has a hash")
    args = parser.parse_args(argv)
    text = REQUIREMENTS.read_text(encoding="utf-8")
    if args.check:
        missing = unhashed(text)
        for name in missing:
            print(f"no hash: {name}")
        return 1 if missing else 0
    REQUIREMENTS.write_text(render(text), encoding="utf-8", newline="\n")
    print(f"{REQUIREMENTS}: {len(pins(text))} pins hashed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
