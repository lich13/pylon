"""Optional request telemetry for a model caller; never stores prompts or keys."""
from contextvars import ContextVar

observer = ContextVar('pylon_model_observer', default=None)


def emit(event, **fields):
    from .model_execution import current
    execution = current.get()
    if execution: execution.observe(event, fields)
    callback = observer.get()
    if callback is not None: callback(event, fields)
