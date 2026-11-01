"""Own the real existing PG verification slot and a marked group until the test kills this owner."""

import json
import subprocess
import sys

import psycopg2

from description_pipeline.owned_process import process_identity, terminate_owned
from description_pipeline.simulation.store import SimulationStore


payload = json.loads(sys.stdin.readline())
store = SimulationStore(lambda: psycopg2.connect(payload["dsn"], options="-csearch_path=" + payload["schema"]))
slot = store.acquire_verification_slot()
assert slot is not None
store.begin_verification(payload["session"], payload["command"], payload["request"], payload["owner"], slot=slot)
helper = subprocess.Popen(
    [sys.executable, "-I", "-B", "-c", "import time;time.sleep(60)", payload["nonce"]],
    start_new_session=True,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
identity = process_identity(helper.pid, payload["nonce"])
try:
    store.transition_verification(payload["session"], payload["command"], payload["owner"], "running", process=identity)
    print(json.dumps(identity), flush=True)
    sys.stdin.read()
finally:
    terminate_owned(identity)
    slot.close()
