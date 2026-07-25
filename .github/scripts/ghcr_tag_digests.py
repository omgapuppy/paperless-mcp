#!/usr/bin/env python3
"""Resolve GHCR tags to manifest digests through the authenticated Packages API."""

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


class PackagesApiError(RuntimeError):
    """The Packages API could not provide an authoritative answer."""


def _next_link(header: str | None) -> str | None:
    if not header:
        return None
    for item in header.split(","):
        parts = [part.strip() for part in item.split(";")]
        if len(parts) < 2 or 'rel="next"' not in parts[1:]:
            continue
        if parts[0].startswith("<") and parts[0].endswith(">"):
            return parts[0][1:-1]
    return None


def resolve_tag_digests(
    *,
    owner: str,
    package: str,
    tags: list[str],
    token: str,
    allow_missing_package: bool = False,
    open_url: OpenUrl = urlopen,
) -> dict[str, str | None]:
    """Return requested tag digests, optionally accepting 404 only for bootstrap."""
    encoded_owner = quote(owner, safe="")
    encoded_package = quote(package, safe="")
    next_url: str | None = (
        f"https://api.github.com/users/{encoded_owner}/packages/container/"
        f"{encoded_package}/versions?per_page=100"
    )
    results: dict[str, str | None] = dict.fromkeys(tags)
    first_page = True

    while next_url is not None:
        request = Request(
            next_url,
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
                link = response.headers.get("Link")
        except HTTPError as exc:
            if first_page and exc.code == 404 and allow_missing_package:
                return results
            raise PackagesApiError(
                f"GitHub Packages API returned HTTP {exc.code}; tag state is unknown"
            ) from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise PackagesApiError(
                f"GitHub Packages API request failed; tag state is unknown: {exc}"
            ) from exc

        if not isinstance(payload, list):
            raise PackagesApiError("GitHub Packages API returned an unexpected response")

        for version in payload:
            if not isinstance(version, dict):
                raise PackagesApiError("GitHub Packages API returned an invalid package version")
            digest = version.get("name")
            metadata = version.get("metadata")
            if not isinstance(digest, str) or not digest.startswith("sha256:"):
                raise PackagesApiError("GitHub Packages API returned an invalid digest")
            if not isinstance(metadata, dict):
                raise PackagesApiError("GitHub Packages API omitted container metadata")
            container = metadata.get("container")
            if not isinstance(container, dict) or not isinstance(container.get("tags"), list):
                raise PackagesApiError("GitHub Packages API omitted container tags")
            for tag in container["tags"]:
                if tag not in results:
                    continue
                current = results[tag]
                if current is not None and current != digest:
                    raise PackagesApiError(f"GHCR tag {tag!r} maps to multiple digests")
                results[tag] = digest

        next_url = _next_link(link)
        first_page = False

    return results


def _write_github_output(results: dict[str, str | None]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise PackagesApiError("GITHUB_OUTPUT is unavailable")
    with Path(output_path).open("a", encoding="utf-8") as output:
        for index, digest in enumerate(results.values()):
            output.write(f"tag_{index}_digest={digest or ''}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--tag", action="append", required=True, dest="tags")
    parser.add_argument("--allow-missing-package", action="store_true")
    parser.add_argument("--github-output", action="store_true")
    arguments = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 2

    try:
        results = resolve_tag_digests(
            owner=arguments.owner,
            package=arguments.package,
            tags=arguments.tags,
            token=token,
            allow_missing_package=arguments.allow_missing_package,
        )
        if arguments.github_output:
            _write_github_output(results)
        else:
            print(json.dumps(results, sort_keys=True))
    except PackagesApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
