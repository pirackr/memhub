"""Entry point: python -m bench <run|download> ..."""

import sys

from bench.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
