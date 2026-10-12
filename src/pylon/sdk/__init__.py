"""Stable API for external Pylon tasks (protocol version 1)."""
from importlib import import_module
from ..config import home, profile_root, profile_env, read_env, redact
from ..outbox import Outbox
API_VERSION = 1

def __getattr__(name):
    if name in {'events', 'agent_tasks', 'software_updates', 'software_inventory', 'transport', 'inline'}:
        return import_module('pylon.' + name)
    raise AttributeError(name)

def serve(handler):
    import contextlib, json, sys
    request = json.load(sys.stdin)
    if request.get('api_version') != API_VERSION or request.get('profile') not in {'default', 'webops'}:
        raise ValueError('unsupported extension request')
    with contextlib.redirect_stdout(sys.stderr):
        value = handler(request['action'], request['profile'], request.get('payload', {}))
    print(json.dumps({'api_version': API_VERSION, 'value': value}, ensure_ascii=False))
