"""Backwards-compatible re-export; the data lives in eccbolt.ck_table."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eccbolt.ck_table import L, N, TABLE  # noqa: F401
