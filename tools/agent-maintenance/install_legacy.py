#!/usr/bin/env python3
"""Replace historical script entries with delegates, retaining timestamped originals."""
import argparse
import datetime
from pathlib import Path
import shutil
import uuid

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--scripts-dir", type=Path, required=True)
args = parser.parse_args()
root = args.scripts_dir.expanduser().resolve()
if not root.is_dir():
    raise SystemExit("The scripts directory does not exist")
source = Path(__file__).resolve().parent
stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
backup = root / ("agent-maintenance-originals-" + stamp)
backup.mkdir(mode=0o700)
destination = root / "agent-maintenance"
if destination.exists():
    destination.rename(backup / "agent-maintenance")
destination.mkdir(mode=0o755)
for name in ("agent_maintenance.py", "host.py", "README.txt", "agent-maintenance", "find-python.sh", "config.example.json"):
    shutil.copyfile(source / name, destination / name)
    (destination / name).chmod(0o755 if name == "agent-maintenance" else 0o644)
for previous, replacement in (("codex_archives.py", "legacy_codex_archives.py"),
                              ("claude_sessions.py", "legacy_claude_sessions.py")):
    path = root / previous
    body = (source / replacement).read_bytes()
    if path.is_file() and path.read_bytes() == body:
        continue
    if path.exists():
        shutil.copy2(path, backup / previous)
    temporary = root / ("." + previous + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(body)
        temporary.chmod(0o755)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
print("Installed delegates in " + str(root))
print("Original scripts retained in " + str(backup))
