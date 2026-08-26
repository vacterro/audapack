#!/usr/bin/env pythonw
"""AUDAPACK GUI launcher (no console window)."""

import sys
from pathlib import Path

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from audapack.app import main

if __name__ == "__main__":
    sys.exit(main())
