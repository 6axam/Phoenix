#!/usr/bin/env python3
"""Trusted fixed entry point for historical text in a newly created Kitty pane."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phoenix.cli import main
raise SystemExit(main(['archive-replay', *sys.argv[1:]]))
