#!/usr/bin/env python3
"""Install the stdlib-only maintenance CLI and preserve any previous installation."""
import argparse
import datetime
from pathlib import Path
import shlex
import shutil
import sys

parser = argparse.ArgumentParser(description=__doc__)
default = (Path.home() / "Library/Application Support/AgentMaintenance/CLI" if sys.platform == "darwin"
           else Path.home() / ".local/share/agent-maintenance")
parser.add_argument("--prefix", type=Path, default=default)
parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
args = parser.parse_args()
stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
prefix = args.prefix.expanduser().resolve()
binary = args.bin_dir.expanduser().resolve() / "agent-maintenance"
if prefix.exists():
    prefix.rename(prefix.with_name(prefix.name + ".backup-" + stamp))
prefix.mkdir(parents=True, mode=0o700)
source = Path(__file__).resolve().parent
for name in ("agent_maintenance.py", "host.py", "README.txt"):
    shutil.copyfile(source / name, prefix / name)
    (prefix / name).chmod(0o600)
binary.parent.mkdir(parents=True, exist_ok=True)
if binary.exists() or binary.is_symlink():
    binary.rename(binary.with_name(binary.name + ".backup-" + stamp))
binary.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " +
                  shlex.quote(str(prefix / "agent_maintenance.py")) + ' "$@"\n')
binary.chmod(0o755)
print("Installed " + str(binary))
