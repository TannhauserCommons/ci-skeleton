"""Command line for hello: the greeting, --version, or --screen."""
import argparse

import hello


def main(argv=None):
    """Print what was asked for and return the exit code."""
    parser = argparse.ArgumentParser(prog="hello", description="Print a greeting.")
    parser.add_argument("--version", action="store_true", help="print the version")
    parser.add_argument("--screen", action="store_true", help="print the key screen")
    args = parser.parse_args(argv)
    if args.version:
        print(hello.version())
    elif args.screen:
        print(hello.SCREEN)
    else:
        print(hello.greeting())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
