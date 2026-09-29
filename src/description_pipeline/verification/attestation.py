"""Authenticate external acceptance against artifacts from explicitly trusted CI."""

from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import zipfile
from pathlib import Path

from ..io import read_data

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


def _api(endpoint: str) -> bytes:
    response = subprocess.run(["gh", "api", endpoint], capture_output=True, check=True, timeout=30)
    if len(response.stdout) > MAX_ARTIFACT_BYTES:
        raise ValueError("Acceptance artifact exceeds size limit")
    return response.stdout


def verify_external_record(record: dict, purpose: str) -> dict:
    reference = record.get("attestation")
    if not isinstance(reference, dict):
        return {
            "trusted": False,
            "status": "not_run",
            "reason": "External acceptance requires a trusted execution artifact",
        }
    try:
        repository = reference["repository"]
        if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid attestation repository")
        for key in ("run_id", "artifact_id"):
            if type(reference[key]) is not int or reference[key] <= 0:
                raise ValueError("Invalid attestation identity")
        policy = read_data(Path(__file__).with_name("acceptance_trust.json"))
        sources = [source for source in policy["sources"] if source["repository"] == repository]
        if policy["schema_version"] != "description.acceptance-trust/v1" or not sources:
            raise ValueError("Acceptance producer is not registered in the locked tool's trust policy")
        prefix = f"repos/{repository}/actions"
        run = json.loads(_api(f"{prefix}/runs/{reference['run_id']}"))
        if not any(
            run.get("path") == source["workflow"] and run.get("head_branch") == source["branch"] for source in sources
        ):
            raise ValueError("Acceptance workflow or branch is not trusted")
        if (
            run.get("conclusion") != "success"
            or run.get("event") != "workflow_dispatch"
            or run.get("display_title") != f"accept {record['subject']} ({purpose})"
        ):
            raise ValueError("Acceptance run does not qualify this exact model subject and purpose")
        artifact = json.loads(_api(f"{prefix}/artifacts/{reference['artifact_id']}"))
        if artifact.get("expired") or artifact.get("workflow_run", {}).get("id") != reference["run_id"]:
            raise ValueError("Acceptance artifact is expired or belongs to another run")
        archive = _api(f"{prefix}/artifacts/{reference['artifact_id']}/zip")
        if artifact.get("digest") != "sha256:" + hashlib.sha256(archive).hexdigest():
            raise ValueError("Acceptance artifact digest mismatch")
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            entries = package.infolist()
            if sum(item.file_size for item in entries) > MAX_ARTIFACT_BYTES or len(
                {item.filename for item in entries}
            ) != len(entries):
                raise ValueError("Ambiguous or oversized acceptance archive")
            payload = {key: value for key, value in record.items() if key != "attestation"}
            if json.loads(package.read("acceptance.json")) != payload:
                raise ValueError("Candidate acceptance record differs from the trusted producer's artifact")
            for item in payload["results"]:
                for name, checksum in item["artifacts"].items():
                    if hashlib.sha256(package.read(name)).hexdigest() != checksum:
                        raise ValueError("Acceptance log differs from the trusted producer's artifact")
        return {
            "trusted": True,
            "status": "passed",
            "run_url": f"https://github.com/{repository}/actions/runs/{reference['run_id']}",
        }
    except (OSError, subprocess.SubprocessError):
        return {
            "trusted": False,
            "status": "not_run",
            "reason": "Trusted acceptance could not be retrieved; authenticate gh and check connectivity",
        }
    except (KeyError, TypeError, ValueError, zipfile.BadZipFile) as error:
        return {"trusted": False, "status": "failed", "reason": str(error)}
