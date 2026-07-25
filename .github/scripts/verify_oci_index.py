#!/usr/bin/env python3
"""Verify a staged OCI index's digest and release identity annotations."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


class IndexVerificationError(RuntimeError):
    """An image index does not identify the intended release."""


def verify_index(
    raw: bytes,
    *,
    digest: str,
    revision: str,
    version: str,
) -> None:
    actual_digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    if actual_digest != digest:
        raise IndexVerificationError(
            f"index content digest {actual_digest} does not match expected {digest}"
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IndexVerificationError("registry returned invalid index JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("manifests"), list):
        raise IndexVerificationError("registry response is not a manifest index")

    annotations = payload.get("annotations")
    if not isinstance(annotations, dict):
        raise IndexVerificationError("image index has no annotations")
    expected = {
        "org.opencontainers.image.revision": revision,
        "org.opencontainers.image.version": version,
    }
    for key, value in expected.items():
        if annotations.get(key) != value:
            raise IndexVerificationError(f"image index annotation {key!r} does not match")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--version", required=True)
    arguments = parser.parse_args()

    try:
        verify_index(
            arguments.path.read_bytes(),
            digest=arguments.digest,
            revision=arguments.revision,
            version=arguments.version,
        )
    except (OSError, IndexVerificationError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
