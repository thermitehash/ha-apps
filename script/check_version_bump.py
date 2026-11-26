"""Fail a PR that changes an app folder without bumping its version.

For every app folder (a directory with config.yaml) changed against the base
ref: config.yaml's ``version`` must be SemVer, greater than the base
branch's, and CHANGELOG.md must have a ``## <version>`` heading.

Usage: python script/check_version_bump.py <base-ref>
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

VERSION_RE = re.compile(r'^version:\s*"?(\d+)\.(\d+)\.(\d+)"?\s*$', re.MULTILINE)


def run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def parse(text: str) -> tuple[int, int, int] | None:
    m = VERSION_RE.search(text)
    return None if m is None else (int(m[1]), int(m[2]), int(m[3]))


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    changed = run("git", "diff", "--name-only", f"{base}...HEAD").split()
    apps = sorted(
        {p.split("/")[0] for p in changed if "/" in p and pathlib.Path(p.split("/")[0], "config.yaml").is_file()}
    )
    if not apps:
        print("No app folders changed; no version bump needed.")
        return 0
    failed = False
    for app in apps:
        config = pathlib.Path(app, "config.yaml").read_text()
        head = parse(config)
        if head is None:
            print(f"{app}: config.yaml has no X.Y.Z version")
            failed = True
            continue
        try:
            base_cfg = run("git", "show", f"{base}:{app}/config.yaml")
        except subprocess.CalledProcessError:
            base_cfg = ""
        before = parse(base_cfg)
        shown = ".".join(map(str, head))
        if before is not None and head <= before:
            print(f"{app}: version {shown} is not above {'.'.join(map(str, before))} on {base}. Bump config.yaml.")
            failed = True
        changelog = pathlib.Path(app, "CHANGELOG.md")
        if not changelog.is_file() or not re.search(
            rf"^## {re.escape(shown)}\b", changelog.read_text(), re.MULTILINE
        ):
            print(f"{app}: CHANGELOG.md has no '## {shown}' entry")
            failed = True
        if not failed:
            print(f"{app}: {shown} OK (base: {'.'.join(map(str, before)) if before else 'new app'})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
