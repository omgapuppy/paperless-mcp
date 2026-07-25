from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
VALIDATOR = (
    ROOT
    / ".agents"
    / "skills"
    / "paperless-document-management"
    / "scripts"
    / "validate-proposal.py"
)


def _proposal() -> dict[str, Any]:
    return {
        "description": "Retitle one clear invoice",
        "changes": [
            {
                "document_id": 123,
                "expected_current_state": {
                    "title": "attachment.pdf",
                    "tag_ids": [1, 4],
                    "correspondent_id": None,
                    "document_type_id": None,
                },
                "changes": {
                    "title": "Electric Ireland – Electricity Bill – June 2026",
                    "add_tag_ids": [12],
                    "remove_tag_ids": [1],
                    "correspondent_id": 8,
                    "document_type_id": 3,
                },
                "confidence": 0.97,
                "reason": "The issuer, bill heading, and billing period are explicit.",
            }
        ],
    }


def _run(path: Path, *, standalone: bool = False) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(VALIDATOR), str(path)]
    if standalone:
        command.append("--standalone")
    return subprocess.run(command, check=False, capture_output=True, text=True)


def test_validator_accepts_valid_proposal_with_project_model(tmp_path: Path) -> None:
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(_proposal()), encoding="utf-8")

    result = _run(path)

    assert result.returncode == 0
    assert "no network request or mutation" in result.stdout


def test_validator_accepts_valid_proposal_standalone(tmp_path: Path) -> None:
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(_proposal()), encoding="utf-8")

    result = _run(path, standalone=True)

    assert result.returncode == 0
    assert "standalone schema" in result.stdout


def test_validator_rejects_invalid_confidence(tmp_path: Path) -> None:
    proposal = _proposal()
    proposal["changes"][0]["confidence"] = 1.5
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(proposal), encoding="utf-8")

    result = _run(path)

    assert result.returncode == 1
    assert "invalid proposal" in result.stderr


def test_validator_reports_malformed_json_as_input_error(tmp_path: Path) -> None:
    path = tmp_path / "proposal.json"
    path.write_text("{", encoding="utf-8")

    result = _run(path)

    assert result.returncode == 2
    assert "input error" in result.stderr
