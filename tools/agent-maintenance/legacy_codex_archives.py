#!/usr/bin/env python3
"""Compatibility entry point for the unified conversation manager."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent-maintenance"))
from agent_maintenance import main

raise SystemExit(main(["interactive", "--host", "local", "--tool", "codex", "--archived", "yes", *sys.argv[1:]]))
