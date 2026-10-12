"""Launch child work with the same immutable module snapshot."""
from pylon.extensions import python, snapshot
from pylon.extensions import environment as _environment

def environment(profile):
    return _environment(profile, snapshot())
