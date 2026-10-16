#!/usr/bin/env python3
"""Start VSCode Assistant in your browser:  python run_web.py   (or double-click run_web.pyw on Windows).

Options: --port 8765, --no-open, --tab (normal browser tab instead of an app window),
--keep-running (don't stop when the last app window closes).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vscode_assistant.server import main  # noqa: E402

main()
