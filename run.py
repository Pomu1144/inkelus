#!/usr/bin/env python3
"""Start VSCode Assistant:  python run.py   (or double-click run.pyw on Windows)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vscode_assistant.app import main  # noqa: E402

main()
