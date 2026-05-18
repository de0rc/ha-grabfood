#!/usr/bin/env python3
"""Verify that the version string is in sync across all three source files.

Run before committing a version bump:
    python3 check_version.py
"""
import re
import sys

FILES = {
    "config.yaml":                          r'^version:\s+"?([0-9.]+)"?',
    "app/bridge.py":                        r'ADDON_VERSION\s*=\s*"([0-9.]+)"',
    "app/www/grabfood-map-card.template.js": r"const _VERSION\s*=\s*'([0-9.]+)'",
}

versions = {}
for path, pattern in FILES.items():
    try:
        content = open(path).read()
    except FileNotFoundError:
        print(f"ERROR: {path} not found", file=sys.stderr)
        sys.exit(1)
    m = re.search(pattern, content, re.MULTILINE)
    if not m:
        print(f"ERROR: version pattern not found in {path}", file=sys.stderr)
        sys.exit(1)
    versions[path] = m.group(1)

unique = set(versions.values())
if len(unique) == 1:
    print(f"Version OK: {unique.pop()}")
else:
    print("VERSION MISMATCH:", file=sys.stderr)
    for path, ver in versions.items():
        print(f"  {path}: {ver}", file=sys.stderr)
    sys.exit(1)
