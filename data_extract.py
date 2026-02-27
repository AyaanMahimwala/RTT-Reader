"""Import shim for data-extract.py (hyphens aren't valid in Python module names)."""

import importlib

_mod = importlib.import_module("data-extract")

get_raw_calendar_data_with_creds = _mod.get_raw_calendar_data_with_creds
export_to_csv = _mod.export_to_csv
