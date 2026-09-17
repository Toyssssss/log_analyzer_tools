import sys


class _NullWriter:
    """Absorbs stray writes when there is no console.

    In a PyInstaller windowed build on Windows sys.stdout and sys.stderr are
    None, so any remaining print() raises AttributeError. Installed before any
    other import so a debug print cannot kill the app.
    """

    def write(self, s):
        return len(s) if s else 0

    def flush(self):
        pass


if sys.stdout is None:
    sys.stdout = _NullWriter()
if sys.stderr is None:
    sys.stderr = _NullWriter()

from log_analyzer.gui.app import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
