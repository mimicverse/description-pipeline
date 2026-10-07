"""Pin the Airflow test modules to one owned, private AIRFLOW_HOME before Airflow imports.

Airflow reads ``AIRFLOW_HOME`` when its configuration is first imported, so this runs before any
``airflow`` import. The tests always allocate their own fresh temporary home: an ambient
``AIRFLOW_HOME`` may point at a real deployment database or at ``deploy/airflow/home`` inside the
checkout, and neither may be migrated, written or cleaned by a test run.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path


def pinned_airflow_home() -> Path:
    """Allocate the owned temporary AIRFLOW_HOME for this test process, whatever the caller set."""
    home = Path(tempfile.mkdtemp(prefix="description-airflow-home-"))
    atexit.register(shutil.rmtree, home, True)
    os.environ["AIRFLOW_HOME"] = str(home)
    return home
