#!/usr/bin/env python3
"""Entry point: `python run.py scan`, `python run.py run`, ..."""
import sys

from hyperbot.cli import main

if __name__ == "__main__":
    sys.exit(main())
