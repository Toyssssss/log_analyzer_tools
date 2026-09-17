import io
import sys


class _NullWriter(io.TextIOBase):
    """Absorbs stray writes when there is no console.

    In a PyInstaller windowed build on Windows sys.stdout and sys.stderr are
    None, so any remaining print() raises AttributeError. Installing this
    before importing anything else keeps a debug print from killing the app.
    """

    def write(self, _s):
        return len(_s) if _s else 0

    def flush(self):
        pass


def _install_null_streams():
    if sys.stdout is None:
        sys.stdout = _NullWriter()
    if sys.stderr is None:
        sys.stderr = _NullWriter()


def main():
    _install_null_streams()
    from .app import main as app_main
    return app_main()


if __name__ == '__main__':
    sys.exit(main())
