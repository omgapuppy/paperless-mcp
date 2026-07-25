#!/usr/bin/env python3
"""Inspect a GitHub release without conflating absence with API failure."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API_VERSION = "2022-11-28"
OpenUrl = Callable[..., Any]


class ReleasesApiError(RuntimeError):
    """The Releases API could not provide an authoritative answer."""


def release_exists(
    *,
    repository: str,
    tag: str,
    token: str,
    open_url: OpenUrl = urlopen,
) -> bool:
    """Return false only for an authoritative 404; all other errors fail closed."""
    encoded_tag = quote(tag, safe="")
    request = Request(
        f"https://api.github.com/repos/{repository}/releases/tags/{encoded_tag}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "paperless-mcp-release",
        },
    )
    try:
        with open_url(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return False
        raise ReleasesApiError(
            f"GitHub Releases API returned HTTP {exc.code}; release state is unknown"
        ) from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ReleasesApiError(
            f"GitHub Releases API request failed; release state is unknown: {exc}"
        ) from exc

    if not isinstance(payload, dict) or payload.get("tag_name") != tag:
        raise ReleasesApiError("GitHub Releases API returned an unexpected release")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--github-output", action="store_true")
    arguments = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 2

    try:
        exists = release_exists(
            repository=arguments.repository,
            tag=arguments.tag,
            token=token,
        )
        value = str(exists).lower()
        if arguments.github_output:
            output_path = os.environ.get("GITHUB_OUTPUT")
            if not output_path:
                raise ReleasesApiError("GITHUB_OUTPUT is unavailable")
            with Path(output_path).open("a", encoding="utf-8") as output:
                output.write(f"exists={value}\n")
        else:
            print(value)
    except ReleasesApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
