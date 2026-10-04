"""Writable application state; shared with spawned deployment workers."""
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def data_directory():
    configured = os.environ.get("ZTB_DATA_DIR")
    return Path(configured).expanduser().resolve() if configured else ROOT / "out"


def catalog_path():
    if os.environ.get("ZTB_DATA_DIR"):
        return data_directory() / "catalog" / "ucaas_endpoints.json"
    return ROOT / "data" / "ucaas_endpoints.json"
