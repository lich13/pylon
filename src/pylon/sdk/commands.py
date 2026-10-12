"""Stable commands access for external tasks."""
import sys
from importlib import import_module
sys.modules[__name__] = import_module("pylon.commands")
