#!/usr/bin/env python3
"""Decide whether a strict SemVer release may move the mutable latest tag."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

SEMVER_TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


class ReleaseTagError(RuntimeError):
    """The fetched release tags cannot be compared safely."""


def latest_state(current: str, tags: list[str]) -> tuple[bool, str]:
    match = SEMVER_TAG.fullmatch(f"v{current}")
    if match is None:
        raise ReleaseTagError(f"current version is not strict SemVer X.Y.Z: {current!r}")
    current_parts: tuple[int, int, int] = (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
    )

    parsed: list[tuple[tuple[int, int, int], str]] = []
    for tag in tags:
        if not tag:
            continue
        tag_match = SEMVER_TAG.fullmatch(tag)
        if tag_match is None:
            raise ReleaseTagError(f"release-like Git tag is not strict vX.Y.Z: {tag!r}")
        major, minor, patch = map(int, tag_match.groups())
        parsed.append(((major, minor, patch), tag))

    current_tag = f"v{current}"
    if not any(tag == current_tag for _, tag in parsed):
        raise ReleaseTagError(f"current release tag {current_tag!r} was not fetched")

    highest_parts, highest_tag = max(parsed)
    return current_parts >= highest_parts, highest_tag


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current", required=True)
    parser.add_argument("--tags-file", type=Path, required=True)
    parser.add_argument("--github-output", action="store_true")
    arguments = parser.parse_args()

    try:
        is_highest, highest = latest_state(
            arguments.current,
            arguments.tags_file.read_text(encoding="utf-8").splitlines(),
        )
        if arguments.github_output:
            output_path = os.environ.get("GITHUB_OUTPUT")
            if not output_path:
                raise ReleaseTagError("GITHUB_OUTPUT is unavailable")
            with Path(output_path).open("a", encoding="utf-8") as output:
                output.write(f"is_highest={str(is_highest).lower()}\n")
                output.write(f"highest={highest}\n")
        else:
            print(str(is_highest).lower())
    except (OSError, ReleaseTagError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
