from __future__ import annotations

import hashlib
import json
import re
import runpy
import tomllib
from collections.abc import Callable
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError

import pytest
import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]


def test_release_workflow_contract() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "release.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = cast(dict[object, Any], yaml.safe_load(workflow_text))

    assert workflow["permissions"] == {"contents": "write", "packages": "write"}
    assert workflow["concurrency"] == {
        "group": "release-${{ github.repository }}",
        "cancel-in-progress": False,
    }
    assert "id-token:" not in workflow_text
    assert "attestations:" not in workflow_text

    event_value = workflow.get("on")
    if event_value is None:
        event_value = workflow.get(True)
    event = cast(dict[str, Any], event_value)
    pull_request = cast(dict[str, Any], event["pull_request"])
    assert pull_request == {"branches": ["main"], "types": ["closed"]}

    job = cast(dict[str, Any], cast(dict[str, Any], workflow["jobs"])["publish"])
    condition = cast(str, job["if"])
    assert "pull_request.merged == true" in condition
    assert "pull_request.base.ref == 'main'" in condition
    assert "'release'" in condition

    steps = cast(list[dict[str, Any]], job["steps"])
    checkout = next(step for step in steps if step.get("name") == "Check out merged commit")
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.merge_commit_sha }}"
    assert checkout["with"]["fetch-depth"] == 0

    version_step = next(step for step in steps if step.get("name") == "Read and validate version")
    version_script = cast(str, version_step["run"])
    assert "tomllib" in version_script
    assert "semver.fullmatch(value)" in version_script

    build = next(step for step in steps if step.get("uses") == "docker/build-push-action@v6")
    build_config = cast(dict[str, Any], build["with"])
    assert build_config["platforms"] == "linux/amd64,linux/arm64"
    assert build_config["push"] is True
    assert build_config["provenance"] == "mode=max"
    assert build_config["sbom"] is True
    assert build["if"] == "steps.staging.outputs.tag_0_digest == ''"
    annotations = cast(str, build_config["annotations"])
    assert "index:org.opencontainers.image.revision=" in annotations
    assert "index:org.opencontainers.image.version=" in annotations

    tags = cast(str, build_config["tags"])
    assert tags == "${{ env.IMAGE }}:${{ steps.version.outputs.staging_tag }}"

    labels = cast(str, build_config["labels"])
    for label in ("source", "description", "licenses", "version", "revision", "created"):
        assert f"org.opencontainers.image.{label}=" in labels

    step_names = [cast(str, step.get("name", "")) for step in steps]
    assert step_names.index("Inspect merge staging tag") < step_names.index(
        "Build and publish merge staging image"
    )
    assert step_names.index("Select staging digest") < step_names.index(
        "Verify staging index identity"
    )
    assert step_names.index("Validate immutable image tags") < step_names.index(
        "Promote staging digest to immutable tags"
    )
    assert step_names.index("Promote staging digest to immutable tags") < step_names.index(
        "Verify immutable image tags"
    )
    assert step_names.index("Verify immutable image tags") < step_names.index(
        "Create or verify Git tag"
    )
    assert step_names.index("Create or verify Git tag") < step_names.index("Inspect GitHub release")
    assert step_names.index("Create GitHub release with generated notes") < step_names.index(
        "Check whether release may update latest"
    )
    assert step_names.index("Check whether release may update latest") < step_names.index(
        "Promote immutable digest to latest"
    )

    immutable_promotion = next(
        step for step in steps if step.get("name") == "Promote staging digest to immutable tags"
    )
    immutable_script = cast(str, immutable_promotion["run"])
    assert '"${IMAGE}:${VERSION}"' in immutable_script
    assert '"${IMAGE}:${RELEASE_TAG}"' in immutable_script
    assert '"${IMAGE}@${CANDIDATE_DIGEST}"' in immutable_script

    latest_promotion = next(
        step for step in steps if step.get("name") == "Promote immutable digest to latest"
    )
    assert '"${IMAGE}:latest"' in cast(str, latest_promotion["run"])
    assert latest_promotion["if"] == "steps.latest.outputs.is_highest == 'true'"

    tag_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Create or verify Git tag"
    )
    build_index = steps.index(build)
    assert build_index < tag_index
    tag_script = cast(str, steps[tag_index]["run"])
    assert 'git rev-list -n 1 "${RELEASE_TAG}"' in tag_script
    assert '"${tagged_sha}" != "${RELEASE_SHA}"' in tag_script


class _JsonResponse(BytesIO):
    def __init__(self, payload: object) -> None:
        super().__init__(json.dumps(payload).encode())
        self.headers: dict[str, str] = {}


def _json_response(payload: object) -> _JsonResponse:
    return _JsonResponse(payload)


def test_packages_api_distinguishes_absence_from_failure() -> None:
    module = runpy.run_path(str(ROOT / ".github" / "scripts" / "ghcr_tag_digests.py"))
    resolve = cast(
        Callable[..., dict[str, str | None]],
        module["resolve_tag_digests"],
    )
    api_error = cast(type[Exception], module["PackagesApiError"])

    def missing(*args: object, **kwargs: object) -> _JsonResponse:
        raise HTTPError("https://api.github.test", 404, "Not Found", Message(), None)

    assert resolve(
        owner="owner",
        package="package",
        tags=["0.1.0"],
        token="test-token",
        allow_missing_package=True,
        open_url=missing,
    ) == {"0.1.0": None}

    with pytest.raises(api_error):
        resolve(
            owner="owner",
            package="package",
            tags=["0.1.0"],
            token="test-token",
            open_url=missing,
        )

    def unavailable(*args: object, **kwargs: object) -> _JsonResponse:
        raise HTTPError("https://api.github.test", 503, "Unavailable", Message(), None)

    with pytest.raises(api_error):
        resolve(
            owner="owner",
            package="package",
            tags=["0.1.0"],
            token="test-token",
            open_url=unavailable,
        )


def test_packages_api_maps_tags_to_digests() -> None:
    module = runpy.run_path(str(ROOT / ".github" / "scripts" / "ghcr_tag_digests.py"))
    resolve = cast(
        Callable[..., dict[str, str | None]],
        module["resolve_tag_digests"],
    )
    digest = f"sha256:{'a' * 64}"

    def package_page(*args: object, **kwargs: object) -> _JsonResponse:
        return _json_response(
            [
                {
                    "name": digest,
                    "metadata": {"container": {"tags": ["0.1.0", "v0.1.0"]}},
                }
            ]
        )

    assert resolve(
        owner="owner",
        package="package",
        tags=["0.1.0", "v0.1.0", "latest"],
        token="test-token",
        open_url=package_page,
    ) == {"0.1.0": digest, "v0.1.0": digest, "latest": None}


def test_releases_api_distinguishes_absence_from_failure() -> None:
    module = runpy.run_path(str(ROOT / ".github" / "scripts" / "github_release_state.py"))
    exists = cast(Callable[..., bool], module["release_exists"])
    api_error = cast(type[Exception], module["ReleasesApiError"])

    def response(*args: object, **kwargs: object) -> _JsonResponse:
        return _json_response({"tag_name": "v0.1.0"})

    assert exists(
        repository="owner/repository",
        tag="v0.1.0",
        token="test-token",
        open_url=response,
    )

    def missing(*args: object, **kwargs: object) -> _JsonResponse:
        raise HTTPError("https://api.github.test", 404, "Not Found", Message(), None)

    assert not exists(
        repository="owner/repository",
        tag="v0.1.0",
        token="test-token",
        open_url=missing,
    )

    def forbidden(*args: object, **kwargs: object) -> _JsonResponse:
        raise HTTPError("https://api.github.test", 403, "Forbidden", Message(), None)

    with pytest.raises(api_error):
        exists(
            repository="owner/repository",
            tag="v0.1.0",
            token="test-token",
            open_url=forbidden,
        )


def test_oci_index_requires_digest_and_release_annotations() -> None:
    module = runpy.run_path(str(ROOT / ".github" / "scripts" / "verify_oci_index.py"))
    verify = cast(Callable[..., None], module["verify_index"])
    verification_error = cast(type[Exception], module["IndexVerificationError"])
    revision = "a" * 40
    payload = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [],
            "annotations": {
                "org.opencontainers.image.revision": revision,
                "org.opencontainers.image.version": "0.1.0",
            },
        },
        separators=(",", ":"),
    ).encode()
    digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"

    verify(payload, digest=digest, revision=revision, version="0.1.0")
    with pytest.raises(verification_error):
        verify(payload, digest=digest, revision="b" * 40, version="0.1.0")
    with pytest.raises(verification_error):
        verify(payload, digest=f"sha256:{'0' * 64}", revision=revision, version="0.1.0")


def test_latest_release_state_never_rewinds() -> None:
    module = runpy.run_path(str(ROOT / ".github" / "scripts" / "latest_release_state.py"))
    latest_state = cast(Callable[[str, list[str]], tuple[bool, str]], module["latest_state"])
    release_tag_error = cast(type[Exception], module["ReleaseTagError"])

    assert latest_state("0.2.0", ["v0.1.0", "v0.2.0"]) == (True, "v0.2.0")
    assert latest_state("0.1.0", ["v0.1.0", "v0.2.0"]) == (False, "v0.2.0")
    with pytest.raises(release_tag_error):
        latest_state("0.2.0", ["v0.2.0-rc.1"])


def test_release_version_is_strict_semver() -> None:
    with (ROOT / "pyproject.toml").open("rb") as file:
        project = cast(dict[str, Any], tomllib.load(file)["project"])

    version = cast(str, project["version"])
    assert re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version)
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{version}] -" in changelog
    assert f"[{version}]: https://github.com/omgapuppy/paperless-mcp/releases/tag/v{version}" in (
        changelog
    )


def test_codex_docker_examples_are_hardened_and_parseable() -> None:
    example = tomllib.loads(
        (ROOT / "examples" / "codex-config.toml.example").read_text(encoding="utf-8")
    )
    servers = cast(dict[str, dict[str, Any]], example["mcp_servers"])

    for name in ("paperless_docker_env", "paperless_docker_token_file"):
        args = cast(list[str], servers[name]["args"])
        assert args[:3] == ["run", "--rm", "-i"]
        assert "--read-only" in args
        assert "--tmpfs" in args
        assert "--mount" in args
        assert "type=bind,src=/absolute/path/to/paperless-audit,dst=/data/audit" in args
        assert "no-new-privileges" in args
        assert "ALL" in args
        assert "ghcr.io/omgapuppy/paperless-mcp:latest" in args
        assert args[-1] == "mcp"

    env_args = cast(list[str], servers["paperless_docker_env"]["args"])
    assert "PAPERLESS_API_TOKEN" in env_args
    assert not any(argument.startswith("PAPERLESS_API_TOKEN=") for argument in env_args)

    file_args = cast(list[str], servers["paperless_docker_token_file"]["args"])
    assert (
        "type=bind,src=/absolute/private/path/paperless-api-token,"
        "dst=/run/secrets/paperless_api_token,readonly"
    ) in file_args
    assert "PAPERLESS_API_TOKEN_FILE=/run/secrets/paperless_api_token" in file_args


def test_docker_context_and_compose_secret_are_restricted() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert dockerignore[0] == "**"
    assert "!src/**" in dockerignore
    assert "!tests/**" in dockerignore
    assert not any(".env" in line for line in dockerignore)

    compose = cast(
        dict[str, Any],
        yaml.safe_load((ROOT / "examples" / "docker-compose.yml").read_text(encoding="utf-8")),
    )
    service = cast(dict[str, Any], cast(dict[str, Any], compose["services"])["paperless-mcp"])
    assert service["secrets"] == ["paperless_api_token"]
    compose_text = (ROOT / "examples" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "root:10001" in compose_text
    assert "0440" in compose_text
