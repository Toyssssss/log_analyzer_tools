import sys


def main():
    from .cli import main as cli_main
    return cli_main()


if __name__ == '__main__':
    sys.exit(main())
