#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Build rt as one standalone executable for this computer's platform, for a release download.
# Usage: scripts/build-rt.sh <version> <platform-name> <out-dir>
#   e.g. scripts/build-rt.sh 0.1.0 linux-x86_64 dist
# Uses this Python (3.13) and PyInstaller with its dependencies from requirements/release.txt
# (pinned by version and SHA-256, wheels only) in a throwaway virtual environment, checks that the
# executable runs, and writes <out-dir>/rt-<version>-<platform-name>.tar.gz holding rt, Rootkeel's
# LICENSE and THIRD_PARTY_NOTICES.md, and the licences of the Python runtime and PyInstaller
# bootloader the executable carries. On Linux, PyInstaller needs objdump (binutils), and the
# executable runs on systems with the C library of the build system or newer, so the release
# workflow builds in a Debian 12 container.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
version=${1:?version required, for example 0.1.0}
platform=${2:?platform name required, for example linux-x86_64}
out=${3:?output directory required}
python=${PYTHON:-python3}
"$python" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))' || { echo 'Build rt with Python 3.13' >&2; exit 1; }
mkdir -p "$out"
out=$(cd "$out" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
"$python" -m venv "$work/venv"
"$work/venv/bin/python" -m pip install --quiet --disable-pip-version-check --require-hashes --only-binary :all: \
  -r "$here/requirements/release.txt"
# PyInstaller takes a script with a .py name.
cp "$here/rt" "$work/rt.py"
# The runtime hook makes the executable trust this computer's certificates (rt-ssl-hook.py).
"$work/venv/bin/pyinstaller" --onefile --name rt --noconfirm --log-level WARN \
  --runtime-hook "$here/scripts/rt-ssl-hook.py" --distpath "$work/dist" --workpath "$work/build" --specpath "$work/build" "$work/rt.py"
"$work/dist/rt" --help | grep -q '^usage: rt ' || { echo 'The built rt does not run' >&2; exit 1; }
package=rt-$version-$platform
mkdir -p "$work/$package/licenses"
cp "$work/dist/rt" "$here/LICENSE" "$here/THIRD_PARTY_NOTICES.md" "$work/$package/"
"$work/venv/bin/python" - "$work/$package/licenses" <<'PY'
import shutil, sys, sysconfig
from importlib import metadata
from pathlib import Path
target = Path(sys.argv[1])
shutil.copy(Path(sysconfig.get_path("stdlib")) / "LICENSE.txt", target / "Python-LICENSE.txt")
copying = [f for f in metadata.files("pyinstaller") if f.name == "COPYING.txt"]
if not copying:
    sys.exit("PyInstaller's COPYING.txt is missing")
shutil.copy(copying[0].locate(), target / "PyInstaller-COPYING.txt")
PY
tar -C "$work" -czf "$out/$package.tar.gz" "$package"
echo "$out/$package.tar.gz"
