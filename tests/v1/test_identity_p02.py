"""P-02 submission identity resolution: targets, suggestion, synthesis and transfer wiring."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.orchestration import portal, uploads
from description_pipeline.orchestration.airflow_client import EndpointProtocolError, _linux_targets
from description_pipeline.orchestration.discovery_transfer import _GENERATED_MEMBERS
from description_pipeline.sources.solidworks import discovery

DIGEST = "a" * 64
CONTROL = "b" * 64
SUGGESTION_TARGETS = {"m3.0": {}, "m2.2a": {}}


def _record(identity: dict | None = None) -> dict:
    return {
        "identity": identity or {},
        "saved_configuration": "Default",
        "properties": {},
        "components": [],
        "datums": [],
        "mates": [],
        "files": {},
    }


class TargetsValidationTests(unittest.TestCase):
    def test_accepts_a_configured_target(self) -> None:
        targets = _linux_targets({"m3.0": {"repository_slug": "mimicverse/description", "base": "main"}})
        self.assertEqual(targets["m3.0"]["repository_slug"], "mimicverse/description")

    def test_rejects_malformed_targets(self) -> None:
        cases = (
            {"m3.0": {"repository_slug": "mimicverse/description"}},
            {"m3.0": {"repository_slug": "no-slash", "base": "main"}},
            {"m3.0": {"repository_slug": "a/b", "base": "--evil"}},
            {"m 3": {"repository_slug": "a/b", "base": "main"}},
            {"m3.0": {"repository_slug": "a/b", "base": "x..y"}},
        )
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(EndpointProtocolError):
                _linux_targets(bad)


class SuggestionTests(unittest.TestCase):
    def test_unique_casefold_containment(self) -> None:
        self.assertEqual(portal.suggest_target(["M3.0_20261010"], SUGGESTION_TARGETS), "m3.0")
        self.assertEqual(portal.suggest_target(["robot", "M2.2A/robot.SLDASM"], SUGGESTION_TARGETS), "m2.2a")

    def test_ambiguous_or_absent(self) -> None:
        self.assertIsNone(portal.suggest_target(["m3.0-m2.2a"], SUGGESTION_TARGETS))
        self.assertIsNone(portal.suggest_target(["robot"], SUGGESTION_TARGETS))
        self.assertIsNone(portal.suggest_target([], SUGGESTION_TARGETS))


class PortalConfigTargetsTests(unittest.TestCase):
    def test_loader_reads_and_validates_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token = root / "endpoint.token"
            token.write_text("t" * 40, encoding="utf-8")
            config = root / "portal.json"
            config.write_text(
                json.dumps(
                    {
                        "airflow": {"url": "http://127.0.0.1:8791"},
                        "endpoint": {"url": "http://127.0.0.1:18765", "token_file": str(token)},
                        "pipeline": {
                            "store_root": str(root / "runs"),
                            "repositories": {"mimicverse/description": str(root / "models")},
                            "targets": {"m3.0": {"repository_slug": "mimicverse/description", "base": "main"}},
                            "discovery": {},
                        },
                        "portal": {"host": "127.0.0.1", "port": 8780},
                    }
                ),
                encoding="utf-8",
            )
            loaded = portal.load_portal_config(config)
            self.assertEqual(loaded.pipeline_targets["m3.0"]["base"], "main")


class IdentitySynthesisTests(unittest.TestCase):
    def _identity(self, cad_identity: dict, submission=None):
        findings: list[dict] = []
        identity, sources = discovery._identity(
            _record(cad_identity),
            discovery.DiscoverySettings(),
            findings,
            configuration="Default",
            assembly="robot.SLDASM",
            native_files={"robot.SLDASM": "x"},
            submission=submission,
            handoff_sha256=DIGEST,
        )
        return identity, sources, {item["code"]: item for item in findings}

    def test_cad_values_win_over_submission(self) -> None:
        cad = {
            "hardware_id": "m3.0",
            "revision": "m3.0-20260101-aaaaaaaa",
            "owner": "cad-owner",
            "change_summary": "cad summary",
            "control": {"system": "handoff", "reference": "cad-ref"},
        }
        submission = discovery.SubmissionIdentity(
            hardware_id="m3.0", owner="portal-user", change_summary="portal summary", as_of="20261013",
            inventory_digest=DIGEST,
        )
        identity, sources, findings = self._identity(cad, submission)
        self.assertEqual(identity["owner"], "cad-owner")
        self.assertEqual(identity["change_summary"], "cad summary")
        self.assertEqual(identity["control"], {"system": "handoff", "reference": "cad-ref"})
        self.assertEqual(sources["owner"], "cad")
        self.assertNotIn("discovery.identity_conflict", findings)

    def test_submitted_hardware_conflict_blocks(self) -> None:
        submission = discovery.SubmissionIdentity(
            hardware_id="m4.0", owner="portal-user", as_of="20261013", inventory_digest=DIGEST
        )
        identity, sources, findings = self._identity({"hardware_id": "m3.0"}, submission)
        self.assertIn("discovery.identity_conflict", findings)
        self.assertTrue(findings["discovery.identity_conflict"].get("blocking", True))
        self.assertEqual(identity["hardware_id"], "m3.0")
        self.assertEqual(sources["hardware_id"], "cad")

    def test_derived_revision_parent_owner_and_control(self) -> None:
        submission = discovery.SubmissionIdentity(
            hardware_id="m3.0",
            owner="portal-user",
            change_summary="修复踝关节零位",
            parent_revision="m3.0-20261001-deadbeef",
            control_reference=CONTROL,
            as_of="20261013",
            inventory_digest=DIGEST,
        )
        identity, sources, findings = self._identity({}, submission)
        self.assertEqual(identity["revision"], f"m3.0-20261013-{DIGEST[:8]}")
        self.assertEqual(identity["parent_revision"], "m3.0-20261001-deadbeef")
        self.assertEqual(identity["owner"], "portal-user")
        self.assertEqual(identity["change_summary"], "修复踝关节零位")
        self.assertEqual(identity["control"], {"system": "handoff", "reference": CONTROL})
        self.assertIn("discovery.identity_derived", findings)
        self.assertEqual(
            sorted(findings["discovery.identity_derived"]["detail"]["fields"]),
            ["change_summary", "control", "hardware_id", "owner", "parent_revision", "revision"],
        )
        self.assertEqual(sources["revision"], "derived")

    def test_absent_identity_stays_deferred(self) -> None:
        identity, sources, findings = self._identity({})
        self.assertIsNone(identity["hardware_id"])
        self.assertIsNone(identity["revision"])
        self.assertEqual(sources["hardware_id"], "absent")
        self.assertIn("discovery.identity_missing", findings)
        self.assertIn("discovery.revision_missing", findings)
        self.assertFalse(findings["discovery.identity_missing"].get("blocking", True))


class TransferWhitelistTests(unittest.TestCase):
    def test_identity_evidence_is_a_transfer_member(self) -> None:
        self.assertIn(discovery.IDENTITY_FILE, _GENERATED_MEMBERS)


class UploadFieldTests(unittest.TestCase):
    def _receiver(self, root: Path) -> uploads._Receiver:
        staging = root / ".staging"
        staging.mkdir()
        return uploads._Receiver(staging, root)

    def test_target_and_summary_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            receiver = self._receiver(Path(temporary))
            receiver._text_field = uploads.TARGET_HARDWARE_FIELD
            receiver._text_data = bytearray(b" m3.0 ")
            receiver._finish_text()
            self.assertEqual(receiver._target, "m3.0")
            receiver._text_field = uploads.CHANGE_SUMMARY_FIELD
            receiver._text_data = bytearray("修复踝关节零位".encode())
            receiver._finish_text()
            self.assertEqual(receiver._summary, "修复踝关节零位")

    def test_summary_length_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            receiver = self._receiver(Path(temporary))
            receiver._text_field = uploads.CHANGE_SUMMARY_FIELD
            payload = b"x" * (uploads.MAX_CHANGE_SUMMARY_BYTES + 1)
            with self.assertRaises(uploads.UploadRejected):
                receiver._on_part_data(payload, 0, len(payload))
