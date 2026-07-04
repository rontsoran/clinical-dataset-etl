"""Stub out the `config` module before `main` is imported, so the test suite
never needs a real MySQL server or a config.py file — every test in this
suite exercises pure functions (column filtering, type inference, value
normalization) that never touch the database.
"""
import sys
import types

if "config" not in sys.modules:
    fake_config = types.ModuleType("config")
    fake_config.DB_CONFIG = {"host": "localhost", "user": "test", "password": "test"}
    sys.modules["config"] = fake_config
