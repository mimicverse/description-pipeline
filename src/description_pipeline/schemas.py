"""Single definitions for record schema ids shared across pipeline boundaries.

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
CAD_REVISION_SCHEMA = "solidworks-to-urdf.cad-revision/v1"
