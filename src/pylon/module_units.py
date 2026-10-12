"""Trusted systemd templates for validated external task descriptors."""
from pathlib import Path

def calendar(expression):
    minute, hour, day, month, weekday = expression.split()
    if month != '*' or weekday not in {'*', '0', '1', '2', '3', '4', '5', '6', '7'}:
        raise ValueError('unsupported schedule')
    def field(value):
        return '0/' + value[2:] if value.startswith('*/') else value
    week = '' if weekday == '*' else ('Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')[int(weekday)] + ' '
    return f'{week}*-*-{field(day)} {field(hour)}:{field(minute)}:00 Asia/Shanghai'

def timer_text(key, job):
    schedule = ('OnBootSec=1min\nOnUnitActiveSec=5min\nAccuracySec=30s' if job.get('monotonic') else
                f'OnCalendar={calendar(job["schedule"])}\nPersistent=true\nAccuracySec=1s')
    jitter = '\nRandomizedDelaySec=30m' if job.get('jitter') else ''
    return (f'[Unit]\nDescription=pylon {job["name"]}\n\n[Timer]\n{schedule}{jitter}\n'
            f'Unit=pylon-task@{key}.service\n\n[Install]\nWantedBy=timers.target\n')

def analysis_override(job):
    """Only dedicated model jobs opt out of the normal task deadline."""
    return '[Service]\nTimeoutStartSec=infinity\n' if 'timeout' in job and job['timeout'] is None else ''

def paths(key, job):
    result = {Path('/etc/systemd/system') / f'pylon-job-{key}.timer': timer_text(key, job) if job else None}
    for trigger in ('task', 'manual'):
        path = Path('/etc/systemd/system') / f'pylon-{trigger}@{key}.service.d/model-wait.conf'
        result[path] = analysis_override(job) or None if job else None
    return result
