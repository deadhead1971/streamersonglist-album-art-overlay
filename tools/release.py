"""
Cut a release: bump __version__, commit, tag.

The dashboard's update banner compares the running __version__ against the
latest GitHub release tag. If the two ever drift — tag v1.4.0 without bumping
__init__.py — everyone on 1.4.0 is told forever that 1.4.0 is available. This
script makes the bump and the tag one step so they can't.

This repo releases in two phases: the bump is committed on `test`, then test is
merged into `main` and the tag goes on the merge commit. So does this:

    python -m tools.release 1.4.0 --no-tag   # on test: bump + commit
    git checkout main && git merge test -m "Merge branch 'test': version 1.4.0"
    python -m tools.release --tag-only       # on main: tag the merge commit

For a single-branch flow, the default does both at once:

    python -m tools.release 1.4.0
    python -m tools.release 1.4.0 --dry-run

It stops on a dirty tree or an existing tag, and it does NOT push — pushing the
commit and the tag stays a deliberate act:

    git push && git push origin v1.4.0
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "app" / "__init__.py"

VERSION_RE = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def git(*args, check=True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True,
    )
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def current_version() -> str:
    match = VERSION_RE.search(INIT_PATH.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"No __version__ line found in {INIT_PATH}")
    return match.group(1)


def parse(version: str):
    return tuple(int(part) for part in version.split("."))


def main() -> int:
    ap = argparse.ArgumentParser(description="Bump the version, commit and tag.")
    ap.add_argument("version", nargs="?",
                    help="the new version, e.g. 1.4.0 (no leading v)")
    ap.add_argument("--no-tag", action="store_true",
                    help="bump and commit only — tag the merge commit later")
    ap.add_argument("--tag-only", action="store_true",
                    help="tag HEAD as the version already in app/__init__.py "
                         "(run this on main, on the merge commit)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would happen, change nothing")
    args = ap.parse_args()

    if args.tag_only:
        return tag_only(dry_run=args.dry_run)
    if not args.version:
        raise SystemExit("A version is required unless you pass --tag-only")

    new = args.version.lstrip("v").strip()
    if not SEMVER_RE.match(new):
        raise SystemExit(f"'{args.version}' is not a MAJOR.MINOR.PATCH version")

    old = current_version()
    if parse(new) <= parse(old):
        raise SystemExit(f"{new} is not newer than the current {old}")

    tag = f"v{new}"
    if git("tag", "--list", tag):
        raise SystemExit(f"Tag {tag} already exists")

    dirty = git("status", "--porcelain")
    if dirty:
        raise SystemExit(
            "Working tree is not clean — commit or stash first:\n" + dirty
        )

    print(f"{old} -> {new}")
    if args.dry_run:
        print(f"(dry run) would write {INIT_PATH.name}, commit, and tag {tag}")
        return 0

    text = INIT_PATH.read_text(encoding="utf-8")
    INIT_PATH.write_text(
        VERSION_RE.sub(f'__version__ = "{new}"', text, count=1), encoding="utf-8"
    )

    git("add", str(INIT_PATH.relative_to(ROOT)).replace("\\", "/"))
    git("commit", "-m", f"Bump to {new}")

    if args.no_tag:
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        print(f"Bumped and committed on {branch}. Next:")
        print(f"    git push")
        print(f"    git checkout main && git merge {branch} "
              f"-m \"Merge branch '{branch}': version {new}\"")
        print(f"    python -m tools.release --tag-only")
        return 0

    git("tag", "-a", tag, "-m", tag)
    print(f"Committed and tagged {tag}. To publish:")
    print(f"    git push && git push origin {tag}")
    print(_notes_url(tag))
    return 0


def tag_only(dry_run: bool = False) -> int:
    """
    Tag HEAD with the version already in app/__init__.py.

    This is the second phase: the bump is committed on test, test is merged into
    main, and the tag belongs on that merge commit — which is where every
    previous release tag in this repo sits.
    """
    version = current_version()
    tag = f"v{version}"
    if git("tag", "--list", tag):
        raise SystemExit(f"Tag {tag} already exists")

    dirty = git("status", "--porcelain")
    if dirty:
        raise SystemExit("Working tree is not clean:\n" + dirty)

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    subject = git("log", "-1", "--pretty=%h %s")
    if dry_run:
        print(f"(dry run) would tag {tag} on {branch} @ {subject}")
        return 0

    git("tag", "-a", tag, "-m", tag)
    print(f"Tagged {tag} on {branch} @ {subject}")
    print(f"    git push && git push origin {tag}")
    print(_notes_url(tag))
    return 0


def _notes_url(tag: str) -> str:
    return ("Then draft the release notes at:\n"
            "    https://github.com/deadhead1971/streamersonglist-album-art-overlay"
            f"/releases/new?tag={tag}")


if __name__ == "__main__":
    sys.exit(main())
