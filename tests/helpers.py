"""Load fresh module instances so legacy unittest patches remain isolated."""
from importlib.util import find_spec, spec_from_file_location, module_from_spec
import sys
MODULES = {'chiphell_fid26_monitor.py': 'pylon.tasks.chiphell.monitor', 'chiphell_fid26_proxy_poller.py': 'pylon.tasks.chiphell.poller', 'chiphell_proxy_runtime.py': 'pylon.tasks.chiphell.runtime', 'xianbao_hermes_worker.py': 'pylon.tasks.xianbao.worker', 'xianbao_alert.py': 'pylon.tasks.xianbao.alert', 'xianbao_retention_cleanup.py': 'pylon.tasks.cleanup.xianbao', 'gmail_cleanup_delete.py': 'pylon.tasks.gmail.cleanup', 'v2ex_notice_monitor.py': 'pylon.tasks.forums.v2ex', 'linuxdo_notice_monitor.py': 'pylon.tasks.forums.linuxdo', 'profile_retention_cleanup.py': 'pylon.tasks.cleanup.profile', 'software_update_monitor.py': 'pylon.tasks.software.monitor', 'software_update_monitor_delivery_replay.py': 'pylon.tasks.software.replay', 'itunes_daily_monitor.py': 'pylon.tasks.itunes.monitor', 'itunes_daily_noagent.py': 'pylon.tasks.itunes.report'}

def load_script_module(name, script_name):
    target = MODULES[script_name]
    unique = target.rsplit('.', 1)[0] + '._test_' + name
    spec = spec_from_file_location(unique, find_spec(target).origin)
    module = module_from_spec(spec)
    sys.modules[unique] = module
    spec.loader.exec_module(module)
    return module
