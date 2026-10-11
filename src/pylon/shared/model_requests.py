"""Optional request telemetry for a bounded caller; never stores prompts or keys."""
from contextvars import ContextVar

observer = ContextVar('pylon_model_observer', default=None)


def emit(event, **fields):
    callback = observer.get()
    if callback is not None: callback(event, fields)
