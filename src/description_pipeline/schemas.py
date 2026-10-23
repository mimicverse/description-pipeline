"""Single definitions for record schema ids and native-record predicates crossing boundaries.

Each id is written by one boundary and read by others (VER-04); declaring it here
keeps producer and verifier seams from drifting while the strings that seal
artifacts stay byte-identical.  Import the named constant instead of quoting the
literal.
"""

from __future__ import annotations

INPUT_SCHEMA = "solidworks-to-urdf.input/v1"
TOOL_SCHEMA = "solidworks-to-urdf.tool/v1"
EFFECTIVE_INSTANCES_SCHEMA = "solidworks-to-urdf.effective-instances/v1"
LINK_GROUP_MASS_SCHEMA = "solidworks-to-urdf.link-group-mass-properties/v1"
MASS_CLOSURE_REPORT_SCHEMA = "solidworks-to-urdf.mass-closure-report/v1"
NATIVE_TESSELLATION_SCHEMA = "solidworks-to-urdf.native-tessellation/v8"
NATIVE_DISCOVERY_SCHEMA = "solidworks-to-urdf.native-discovery/v1"
NATIVE_DISCOVERY_FINDING_SCHEMA = "solidworks-to-urdf.discovery-finding/v1"
NATIVE_IDENTITY_RESOLUTION_SCHEMA = "solidworks-to-urdf.identity-resolution/v1"

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

#: Check-strategy machine definition shipped with the release (contracts system contract:
#: schema + stage definition + check policy).  Owner B; consumers reference this id
#: instead of quoting an arbitrary policy string.
CHECK_POLICY_SCHEMA = "solidworks-to-urdf.check-policy/v1"

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
