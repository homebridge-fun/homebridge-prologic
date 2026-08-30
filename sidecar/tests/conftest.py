"""Make `pool_service` importable from the tests without installing it."""
import pathlib
import sys

SIDECAR = pathlib.Path(__file__).resolve().parent.parent
if str(SIDECAR) not in sys.path:
    sys.path.insert(0, str(SIDECAR))
