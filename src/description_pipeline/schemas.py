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
NATIVE_TESSELLATION_SCHEMA = "solidworks-to-urdf.native-tessellation/v8"
NATIVE_DISCOVERY_SCHEMA = "solidworks-to-urdf.native-discovery/v1"
NATIVE_DISCOVERY_FINDING_SCHEMA = "solidworks-to-urdf.discovery-finding/v1"
NATIVE_IDENTITY_RESOLUTION_SCHEMA = "solidworks-to-urdf.identity-resolution/v1"

#: Canonical six-stage records and external-effect envelopes.  These ids cross
#: process and persistence boundaries and therefore have one owner here.
ADMISSION_SPEC_SCHEMA = "solidworks-to-urdf.admission-spec/v2"
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
PUBLICATION_PLAN_SCHEMA = "solidworks-to-urdf.publication-plan/v1"
PUBLICATION_RECEIPT_SCHEMA = "solidworks-to-urdf.publication-receipt/v1"
PUBLISH_REQUEST_SCHEMA = "solidworks-to-urdf.publish-request/v1"
REPOSITORY_SNAPSHOT_SCHEMA = "solidworks-to-urdf.repository-snapshot/v1"
RULESET_CATALOG_SCHEMA = "solidworks-to-urdf.ruleset-catalog/v1"
RUN_ADMISSION_SCHEMA = "solidworks-to-urdf.run-admission/v1"
STAGE_COMMAND_SCHEMA = "solidworks-to-urdf.stage-command/v1"
STAGE_RESULT_SCHEMA = "solidworks-to-urdf.stage-result/v1"
UNHANDLED_STAGE_FAILURE_SCHEMA = "solidworks-to-urdf.unhandled-stage-failure/v1"
VERIFICATION_CERTIFICATE_SCHEMA = "solidworks-to-urdf.verification-certificate/v1"
VERIFICATION_EXECUTION_SCHEMA = "solidworks-to-urdf.verification-execution/v1"
VERIFY_REQUEST_SCHEMA = "solidworks-to-urdf.verify-request/v1"
DISPATCH_CONF_SCHEMA = AIRFLOW_DISPATCH_CONF_SCHEMA
DISPATCH_EFFECT_SCHEMA = AIRFLOW_DISPATCH_EFFECT_SCHEMA

#: W1 machine schemas for records crossing the platform / endpoint boundary
#: (frozen-interfaces-v1.1 IF-04/09/10/11/16).  One owner module; consumers
#: import the constants instead of quoting the literal.
HEALTH_SCHEMA = "solidworks-to-urdf.health/v1"
RESOURCE_POLICY_SCHEMA = "solidworks-to-urdf.resource-policy/v1"
CACHE_RELEASE_SCHEMA = "solidworks-to-urdf.cache-release/v1"
CHECK_POLICY_SCHEMA = "solidworks-to-urdf.check-policy/v1"
CACHE_RELEASE_RECEIPT_SCHEMA = "solidworks-to-urdf.cache-release-receipt/v1"
CACHE_IMPORT_CONFIRMATION_SCHEMA = "solidworks-to-urdf.cache-import-confirmation/v1"
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
