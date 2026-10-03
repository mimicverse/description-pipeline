"""Bind external application/HIL acceptance evidence without pretending to execute it."""

from pathlib import Path

from ..io import PipelineError, confined, digest, file_digest, read_data
from . import result
from .attestation import verify_external_record


def verify_acceptance(
    root: Path, subject: str, profile: dict, *, input_hashes=(), mechanical_reference: Path | None = None
) -> dict:
    required = profile["acceptance_suites"]
    path = root / "docs/acceptance" / (profile["purpose"] + ".json")
    if not required or not path.is_file() or not profile.get("consumer_environment"):
        return result(
            "consumer.application",
            False,
            status="not_run",
            expected=required,
            details="Declare acceptance suites and environment; bind evidence to this exact subject and profile",
        )
    record = read_data(path)
    if not isinstance(record, dict):
        return result("consumer.application", False, details="Invalid acceptance record")
    reference = record.get("attestation")
    if isinstance(reference, dict) and reference.get("kind") == "local_replay":
        from .local_acceptance import verify_replay

        authority = verify_replay(root, record, subject, profile)
    elif isinstance(reference, dict) and reference.get("kind") == "mechanical_reference_replay":
        from .mechanics import verify_replay as verify_mechanics

        authority = verify_mechanics(root, record, subject, profile, mechanical_reference)
    else:
        authority = verify_external_record(record, profile["purpose"])
    checked = []
    valid = (
        record.get("schema_version") == "description.acceptance/v2"
        and record.get("subject") == subject
        and record.get("profile_digest") == digest(profile)
        and record.get("environment") == profile["consumer_environment"]
        and authority["trusted"]
    )
    try:
        for item in record.get("results", []):
            logs = item.get("artifacts", {})
            bound = bool(logs) and all(
                file_digest(confined(root, name)) == value and name.startswith("docs/acceptance/")
                for name, value in logs.items()
            )
            observations = item.get("validation_data", {})
            independent = (
                item.get("data_role") == "validation"
                and item.get("used_for_fitting") is False
                and bool(item.get("conditions"))
                and isinstance(observations, dict)
                and bool(observations)
                and all(
                    name in logs and logs[name] == value and value not in input_hashes
                    for name, value in observations.items()
                )
            )
            permitted = (
                {"physical_measurement"} if profile["purpose"] == "hardware" else {"simulation", "physical_measurement"}
            )
            if profile["purpose"] == "kinematics" and authority.get("execution") == "mechanical_reference_replay":
                permitted.add("reference_replay")
            if (
                item.get("passed") is True
                and bound
                and independent
                and item.get("evidence_class") in permitted
                and all(item.get(key) for key in ("suite_version", "producer", "executed_at"))
            ):
                checked.append(item["suite"])
    except (PipelineError, KeyError, OSError, TypeError, AttributeError):
        valid = False
    return result(
        "consumer.application",
        valid and len(checked) == len(set(checked)),
        expected=required,
        checked=checked,
        status="not_run" if authority["status"] == "not_run" else None,
        details={
            "evidence": str(path.relative_to(root)),
            "execution": authority.get("execution", "external_attestation"),
            "environment": record.get("environment"),
            "required_data_role": "independent validation; no fitting inputs",
            "authority": authority,
        },
    )
