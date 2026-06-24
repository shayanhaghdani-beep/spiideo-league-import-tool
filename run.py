#!/usr/bin/env python3
"""Convenience wrapper so you can run `python3 run.py build …` from this folder
without the `-m league_dataload` invocation."""
from league_dataload.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
