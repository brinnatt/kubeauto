#!/usr/bin/env python3
"""CLI compatibility wrapper for the importable matrix validator."""

from validate_test_matrix import main


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
