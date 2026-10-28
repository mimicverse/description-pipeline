"""Single definitions for record schema ids and native-record predicates crossing boundaries.

Each id is written by one boundary and read by others (VER-04); declaring it here
keeps producer and verifier seams from drifting while the strings that seal
artifacts stay byte-identical.  Import the named constant instead of quoting the
literal.
"""

from __future__ import annotations

INPUT_SCHEMA = "solidworks-to-urdf.input/v1"
ENGINEERING_MODEL_SCHEMA = "solidworks-to-urdf.engineering-model/v1"
TOOL_SCHEMA = "solidworks-to-urdf.tool/v1"
EFFECTIVE_INSTANCES_SCHEMA = "solidworks-to-urdf.effective-instances/v1"
LINK_GROUP_MASS_SCHEMA = "solidworks-to-urdf.link-group-mass-properties/v1"
NATIVE_TESSELLATION_SCHEMA = "native-triangles/v1"
NATIVE_TRIANGLE_STREAM_VERSION = 1
NATIVE_TRIANGLE_STREAM_MAGIC = b"SWTESS64        "
NATIVE_CAPTURE_FACTS_KEYS = frozenset(
    {
        "scene_raw",
        "part_mass_raw",
        "assembly_mass_raw",
        "component_mass_raw",
        "link_group_mass_raw",
        "source_scopes",
        "notes",
    }
)
NATIVE_SCENE_RAW_KEYS = frozenset(
    {
        "document",
        "configuration",
        "occurrences",
        "components",
        "coordinate_systems",
    }
)
NATIVE_CAPTURE_OBSERVATION_SCHEMA = (
    "solidworks-to-urdf.native-capture-facts/v2"
)
NATIVE_CAPTURE_OBSERVATION_KEYS = frozenset(
    {
        "schema_version",
        *NATIVE_CAPTURE_FACTS_KEYS,
        "geometry_raw",
        "axis_references_raw",
        "document_state",
        "input_stability",
    }
)
CAPTURE_EVIDENCE_REQUESTS = (
    "evidence/native-runtime.json",
    "evidence/raw/native_capture_facts.json",
    "evidence/raw/dependency_closure.json",
    "evidence/raw/triangles/",
)
NATIVE_DISCOVERY_SCHEMA = "solidworks-to-urdf.native-discovery/v1"
SAVED_NATIVE_REFERENCE_RULE = "inspection_saved_native_carrier/v1"
SAVED_NATIVE_REFERENCE_POSITION_TOL_M = 1e-7
SAVED_NATIVE_REFERENCE_DOT_TOL = 1e-8
NATIVE_DISCOVERY_ENVELOPE_KEYS = frozenset(
    {
        "schema_version",
        "contract",
        "namespace",
        "run_id",
        "generator",
        "generator_version",
        "handoff_sha256",
        "native_files",
        "derivation",
        "identity",
        "records",
        "derived_interface_names",
        "raw",
        "findings",
    }
)
NATIVE_DISCOVERY_OPTIONAL_ENVELOPE_KEYS = frozenset({"budget"})
NATIVE_DISCOVERY_IDENTITY_KEYS = frozenset(
    {
        "hardware_id",
        "revision",
        "parent_revision",
        "owner",
        "change_summary",
        "control",
        "delivery_configuration",
        "main_assembly",
        "robot_name",
        "robot_name_technical",
    }
)
NATIVE_DISCOVERY_CONTROL_KEYS = frozenset({"system", "reference"})
NATIVE_DISCOVERY_DERIVATION_KEYS = frozenset(
    {"record_roots", "main_assembly", "configuration"}
)
NATIVE_DISCOVERY_RECORD_KEYS = frozenset(
    {
        "reference",
        "file",
        "key",
        "package_file",
        "sha256",
        "source_sha256",
        "anchor",
    }
)
NATIVE_DISCOVERY_BUDGET_KEYS = frozenset(
    {"reference", "key", "package_file", "sha256"}
)
NATIVE_DISCOVERY_FINDING_SCHEMA = "solidworks-to-urdf.discovery-finding/v1"
NATIVE_IDENTITY_RESOLUTION_SCHEMA = "solidworks-to-urdf.identity-resolution/v1"

#: Canonical six-stage records and external-effect envelopes.  These ids cross
#: process and persistence boundaries and therefore have one owner here.
ADMISSION_SPEC_SCHEMA = "solidworks-to-urdf.admission-spec/v3"
AIRFLOW_DISPATCH_CONF_SCHEMA = "solidworks-to-urdf.airflow-dispatch-conf/v1"
AIRFLOW_DISPATCH_EFFECT_SCHEMA = "solidworks-to-urdf.airflow-dispatch-effect/v1"
ARTIFACT_MANIFEST_SCHEMA = "solidworks-to-urdf.artifact-manifest/v1"
CAPTURE_EFFECT_FAILURE_SCHEMA = "solidworks-to-urdf.capture-effect-failure/v1"
DISCOVER_CHECKS_SCHEMA = "solidworks-to-urdf.discover-checks/v1"
DISCOVER_REQUEST_SCHEMA = "solidworks-to-urdf.discover-request/v1"
DISCOVER_RESULT_SCHEMA = "solidworks-to-urdf.discover-result/v1"
DISPATCH_COMMAND_SCHEMA = "solidworks-to-urdf.dispatch-command/v1"
FAILURE_ENVELOPE_SCHEMA = "solidworks-to-urdf.failure-envelope/v1"
FREEZE_CHECKS_SCHEMA = "solidworks-to-urdf.freeze-checks/v1"
FREEZE_REQUEST_SCHEMA = "solidworks-to-urdf.freeze-request/v1"
GENERATE_REQUEST_SCHEMA = "solidworks-to-urdf.generate-request/v1"
GENERATE_RESULT_SCHEMA = "solidworks-to-urdf.generate-result/v1"
NATIVE_CAPTURE_EFFECT_SCHEMA = "solidworks-to-urdf.native-capture-effect/v1"
NATIVE_DISCOVER_EFFECT_SCHEMA = "solidworks-to-urdf.native-discover-effect/v1"
NATIVE_DISCOVER_REQUEST_SCHEMA = "solidworks-to-urdf.native-discover-request/v1"
NATIVE_OBSERVATION_SCHEMA = "solidworks-to-urdf.native-observation/v1"
NATIVE_SOURCE_SET_SCHEMA = "solidworks-to-urdf.native-source-set/v1"
NATIVE_TOOL_REQUIREMENT_KIND = "native_tool_requirement"
PUBLICATION_PLAN_SCHEMA = "solidworks-to-urdf.publication-plan/v1"
PUBLICATION_RECEIPT_SCHEMA = "solidworks-to-urdf.publication-receipt/v1"
PUBLISH_REQUEST_SCHEMA = "solidworks-to-urdf.publish-request/v1"
REPOSITORY_SNAPSHOT_SCHEMA = "solidworks-to-urdf.repository-snapshot/v1"
RULESET_CATALOG_SCHEMA = "solidworks-to-urdf.ruleset-catalog/v1"
RUN_ADMISSION_SCHEMA = "solidworks-to-urdf.run-admission/v1"
STAGE_COMMAND_SCHEMA = "solidworks-to-urdf.stage-command/v1"
STAGE_EFFECT_WAIT_SCHEMA = "solidworks-to-urdf.stage-effect-wait/v1"
STAGE_RESULT_SCHEMA = "solidworks-to-urdf.stage-result/v1"
UNHANDLED_STAGE_FAILURE_SCHEMA = "solidworks-to-urdf.unhandled-stage-failure/v1"
VERIFICATION_CERTIFICATE_SCHEMA = "solidworks-to-urdf.verification-certificate/v1"
VERIFICATION_EXECUTION_SCHEMA = "solidworks-to-urdf.verification-execution/v1"
VERIFY_REQUEST_SCHEMA = "solidworks-to-urdf.verify-request/v1"
DIAGNOSTIC_ADMISSION_SCHEMA = "solidworks-to-urdf.diagnostic-admission-spec/v1"
DIAGNOSTIC_SCOPE_SCHEMA = "solidworks-to-urdf.diagnostic-replay-scope/v1"
DIAGNOSTIC_COMMAND_REQUEST_SCHEMA = "solidworks-to-urdf.diagnostic-command-request/v1"
EFFECT_RECONCILIATION_SCHEMA = "solidworks-to-urdf.effect-reconciliation/v1"
EFFECT_RECONCILIATION_FAILURE_SCHEMA = "solidworks-to-urdf.effect-reconciliation-failure/v1"
NATIVE_CAPTURE_OPERATION_SCHEMA = "solidworks-to-urdf.native-capture-operation/v1"
NATIVE_OPERATION_REQUEST_SCHEMA = "solidworks-to-urdf.native-operation-request/v2"
NATIVE_OPERATION_VIEW_SCHEMA = "solidworks-to-urdf.native-operation-view/v3"
NATIVE_OPERATION_OBSERVATION_SCHEMA = "solidworks-to-urdf.native-operation-observation/v1"
NATIVE_OPERATION_IDENTITY_SCHEMA = "solidworks-to-urdf.native-operation-identity/v1"
RUN_ACTIVITY_SCHEMA = "solidworks-to-urdf.run-activity/v1"
CANCEL_REQUEST_SCHEMA = "solidworks-to-urdf.cancel-request/v1"
OPERATION_CANCEL_EFFECT_SCHEMA = "solidworks-to-urdf.operation-cancel-effect/v1"
PRODUCTION_ADMISSION_INTENT_SCHEMA = "solidworks-to-urdf.production-admission-intent/v1"
NATIVE_TARGET_IDENTITY_SCHEMA = "solidworks-to-urdf.native-target-identity/v1"
NATIVE_TOOL_IDENTITY_SCHEMA = "solidworks-to-urdf.native-tool-identity/v1"
STAGE_QUALIFICATION_SCHEMA = "solidworks-to-urdf.stage-qualification/v1"

#: W1 machine schemas for records crossing the platform / endpoint boundary
#: (frozen-interfaces-v1.1 IF-04/09/11/16).  One owner module; consumers
#: import the constants instead of quoting the literal.
HEALTH_SCHEMA = "solidworks-to-urdf.health/v1"
RESOURCE_POLICY_SCHEMA = "solidworks-to-urdf.resource-policy/v1"
CHECK_POLICY_SCHEMA = "solidworks-to-urdf.check-policy/v1"
CONFIG_SNAPSHOT_SCHEMA = "solidworks-to-urdf.config-snapshot/v1"
INSTALL_RECORD_SCHEMA = "solidworks-to-urdf.install-record/v1"

#: Delivery-tree records owned alongside the delivery boundary (IF-13):
#: ``manifest.json`` (subject inventory, subject digest, contract/policy
#: identity) and ``input/source.json`` (content identity, selection, metadata
#: provenance).
DELIVERY_MANIFEST_SCHEMA = "solidworks-to-urdf.delivery-manifest/v1"
SOURCE_RECORD_SCHEMA = "solidworks-to-urdf.source-record/v1"

#: Platform configuration schema rendered into the immutable config snapshot
#: (deployment.md configuration authority; consumed by IF-11/IF-16).
PLATFORM_CONFIG_SCHEMA = "solidworks-to-urdf.platform/v1"

#: Qualification record machine schema (quality warnings and qualification, IF-14): a policy
#: fact plus its three-state conclusion and its evidence references.
QUALIFICATION_SCHEMA = "solidworks-to-urdf.qualification/v1"

def exporter_declaration_present(record: object) -> bool:
    """The shared producer/verifier predicate over the raw record's exporter block.

    The native capture writes the exporter block unconditionally, so presence alone
    carries no claim: only a proven ``absent`` state means the CAD carries no saved
    v1.4 exporter declaration.  Every other present state (including ``unread``)
    keeps the declaration suite, which fails closed on unreadable declarations; a
    proven ``absent`` state runs the mate-inference suite.
    """

    if not isinstance(record, dict):
        return False
    block = record.get("exporter")
    return "exporter" in record and not (isinstance(block, dict) and block.get("state") == "absent")


def saved_native_reference_present(record: object, purpose: str) -> bool:
    """Route native inspection features; their independent proof still must pass."""
    if purpose != "inspection_preview" or not isinstance(record, dict) or exporter_declaration_present(record):
        return False
    datums = record.get("datums")
    axes = record.get("reference_axes")
    return any(
        isinstance(row, dict) and str(row.get("name") or "").startswith("c_")
        for row in (datums if isinstance(datums, list) else [])
    ) or any(
        isinstance(row, dict) and isinstance(row.get("selector"), dict)
        and str(row["selector"].get("feature_name") or "").startswith("a_")
        for row in (axes if isinstance(axes, list) else [])
    )
