#!/usr/bin/env python3
"""Verify update protection and the bundled SSH helper in an actual signed app."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("app", type=Path)
args = parser.parse_args()
app = args.app.resolve()
info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
assert info.get("AgentSpeedCustomBuild") is True, "Custom build marker is missing"
source = Path(__file__).resolve().parents[1] / "Resources/SSHUsage/sync_usage.py"
bundled = app / "Contents/Resources/SSHUsage/sync_usage.py"
assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(bundled.read_bytes()).digest()
subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
result = subprocess.run(
    [str(app / "Contents/MacOS/AgentSpeed"), "--verify-custom-update-protection"],
    check=True, capture_output=True, text=True, timeout=30,
)
report = json.loads(result.stdout.strip())
assert all(report.values()), report
print(json.dumps(report, indent=2))
