"""Legacy import name forwarding to the installed public package."""

from description_pipeline.verification import urdf_quality as _implementation

__path__ = _implementation.__path__
SCHEMA = _implementation.SCHEMA
