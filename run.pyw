#!/usr/bin/env python3
"""Start Inkelus:  python run.py   (or double-click run.pyw on Windows)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from inkelus.app import main  # noqa: E402

main()
