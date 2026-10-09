"""Synthetic stage observations for transport tests; never native acceptance evidence."""

from datetime import UTC, datetime

from description_pipeline.stages import CONTRACT, STAGE_IDS


def protocol_events(*, stages=STAGE_IDS, subject=None, failed_stage=None):
    events = []
    for definition in CONTRACT["stages"]:
        stage = definition["id"]
        if stage not in stages:
            continue
        for boundary in ("input", "output"):
            for check in definition[f"{boundary}_qc"]:
                failed = stage == failed_stage and boundary == "output"
                events.append(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "stage": stage,
                        "state": "failed" if failed else "running",
                        "check": {
                            "id": check["id"],
                            "boundary": boundary,
                            "state": "failed" if failed else "passed",
                            "details": {
                                "scope": "Synthetic transport protocol; no native qualification",
                                **({"subject_sha256": subject} if subject else {}),
                            },
                        },
                    }
                )
                if failed:
                    return events
        events.append({"at": datetime.now(UTC).isoformat(), "stage": stage, "state": "completed"})
    return events
