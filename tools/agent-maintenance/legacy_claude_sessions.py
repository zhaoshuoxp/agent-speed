#!/usr/bin/env python3
"""Preserve the historical --archived and --filter options."""
import argparse
from pathlib import Path
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--archived", action="store_true")
parser.add_argument("--filter", default="")
options, remaining = parser.parse_known_args()
sys.path.insert(0, str(Path(__file__).resolve().parent / "agent-maintenance"))
from agent_maintenance import main

arguments = ["interactive", "--host", "local", "--tool", "claude", "--query", options.filter,
             "--archived", "yes" if options.archived else "no"]
raise SystemExit(main(arguments + remaining))
