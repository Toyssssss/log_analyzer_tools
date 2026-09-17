#!/usr/bin/env python3
"""Zip the built bundles under dist/ with a platform-and-version tag.

Shared by build.bat and build.sh so the two cannot drift into naming the same
build differently. The tag is normalised to uname's vocabulary (x86_64, not
Windows' 'AMD64') because build.sh derives it from uname -m.
"""
import os
import platform
import shutil
import sys

# Run from the repo root (build.bat/build.sh cd there first), so the package
# is importable; the explicit insert keeps a direct run from any cwd working.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from log_analyzer import __version__

# Windows reports AMD64/ARM64 where uname reports x86_64/aarch64.
_ALIASES = {'amd64': 'x86_64', 'x86': 'i686', 'arm64': 'aarch64'}


def tag() -> str:
    system = platform.system().lower()
    os_name = {'windows': 'win', 'darwin': 'macos'}.get(system, 'linux')
    machine = platform.machine().lower()
    return '%s-%s' % (os_name, _ALIASES.get(machine, machine))


def main() -> int:
    label = '%s-%s' % (tag(), __version__)
    made = []
    for bundle in ('Cluster-log-analyzer', 'Cluster-log-analyzer-cli'):
        base = 'dist/%s-%s' % (bundle, label)
        made.append(shutil.make_archive(base, 'zip', 'dist', bundle))
    for path in made:
        print(path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
