"""Shared test config — put the add-on's `app/` dir on sys.path so modules import by bare name."""
import os
import sys

_APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
