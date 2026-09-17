#!/usr/bin/env bash
# Build the Log Analyzer executables for the current platform.
#
# PyInstaller cannot cross-compile: run this on Windows to get Windows
# binaries and on Linux to get Linux binaries. The GitHub Actions workflow
# in .github/workflows/build.yml does both.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! python -c "import PyInstaller" 2>/dev/null; then
    echo "PyInstaller is not installed. Run:"
    echo "    python -m pip install -r requirements-build.txt"
    exit 1
fi

if ! python -c "import tkinter" 2>/dev/null; then
    echo "WARNING: tkinter is missing, the GUI build will fail."
    echo "On Debian/Ubuntu:  sudo apt-get install -y python3-tk"
    echo "On RHEL/Fedora:    sudo dnf install -y python3-tkinter"
    exit 1
fi

echo "==> running tests"
python -m unittest discover -s tests -t . || {
    echo "tests failed; refusing to build"
    exit 1
}

echo "==> building GUI (onedir, windowed)"
python -m PyInstaller --clean --noconfirm packaging/log-analyzer.spec

echo "==> building CLI"
python -m PyInstaller --clean --noconfirm packaging/log-analyzer-cli.spec

echo "==> packaging dist archives"
python packaging/make_zips.py

echo
echo "Done. Artifacts in dist/:"
ls -1 dist/ | grep -v '^Cluster-log-analyzer\(\|-cli\)$' || true
echo
echo "The GUI bundle is dist/Cluster-log-analyzer/ (run the"
echo "'Cluster-log-analyzer' executable inside it). Distribute the whole folder,"
echo "not just the executable."
