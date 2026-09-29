"""Root-owned SSH maintenance. Inputs never select a path, executable or URL."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import fcntl
import gzip
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from zoneinfo import ZoneInfo

CONFIG = Path('/etc/pylon/ssh.json')
CIDRS = Path('/etc/codex-firewall/beijing-ipv4-cidrs.txt')
IGNORE = Path('/etc/fail2ban/jail.d/sshd-admin-allowlist.local')
PREVIOUS = Path('/etc/pylon/ssh-previous.txt')
TRANSACTION = Path('/etc/pylon/ssh-transaction.json')
SET = 'codex_beijing_ipv4'
STAGE = 'pylon_beijing_stage'


def run(args, *, check=True, input=None, timeout=45):
    return subprocess.run(args, text=True, capture_output=True, check=check, input=input, timeout=timeout)


def atomic_write(path: Path, data: bytes):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        temp.chmod(0o644)
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def networks(text: str, minimum=1000):
    values = [ipaddress.ip_network(line, strict=True) for line in text.splitlines() if line.strip() and not line.startswith('#')]
    if not minimum <= len(values) <= 200000:
        raise ValueError(f'invalid CIDR count: {len(values)}')
    if any(n.version != 4 or n.prefixlen < 8 or not n.network_address.is_global for n in values):
        raise ValueError('invalid Beijing IPv4 ranges')
    if len(set(values)) != len(values):
        raise ValueError('duplicate CIDRs')
    return values


def learned_ips(config, journal: str):
    trusted = set(config['trusted_fingerprints'])
    pattern = re.compile(r'Accepted publickey for ' + re.escape(config['user']) + r' from (\S+) port \d+.*? (SHA256:[A-Za-z0-9+/]+)(?:\s|$)')
    result = set()
    for match in pattern.finditer(journal):
        if match[2] in trusted:
            result.add(str(ipaddress.ip_address(match[1])))
    return sorted(result, key=lambda v: (ipaddress.ip_address(v).version, int(ipaddress.ip_address(v))))


def recent_ips(config):
    journal = run(['/usr/bin/journalctl', '-u', 'ssh.service', '--since', '30 days ago', '--no-pager', '-o', 'cat'], timeout=90).stdout
    return learned_ips(config, journal)


def member(address, set_name=SET):
    return run(['/usr/sbin/ipset', 'test', set_name, str(ipaddress.ip_address(address))], check=False).returncode == 0


def populate(values):
    run(['/usr/sbin/ipset', 'create', SET, 'hash:net', 'family', 'inet', 'hashsize', '32768', 'maxelem', '200000', '-exist'])
    run(['/usr/sbin/ipset', 'create', STAGE, 'hash:net', 'family', 'inet', 'hashsize', '32768', 'maxelem', '200000', '-exist'])
    run(['/usr/sbin/ipset', 'flush', STAGE])
    run(['/usr/sbin/ipset', 'restore'], input=''.join(f'add {STAGE} {n}\n' for n in values), timeout=90)


def recover():
    if not TRANSACTION.exists():
        return
    old = PREVIOUS.read_bytes()
    populate(networks(old.decode()))
    run(['/usr/sbin/ipset', 'swap', STAGE, SET])
    atomic_write(CIDRS, old)
    run(['/usr/sbin/ipset', 'destroy', STAGE])
    TRANSACTION.unlink()
    PREVIOUS.unlink()


def install(values, protected=(), persist=True):
    # Validate the candidate before touching either the file or the live set.
    data = ''.join(f'{n}\n' for n in values).encode()
    networks(data.decode())
    for address in protected:
        ip = ipaddress.ip_address(address)
        if ip.version == 4 and member(address) and not any(ip in n for n in values):
            raise ValueError(f'candidate would remove trusted management source {address}')
    old = CIDRS.read_bytes()
    if persist:
        atomic_write(PREVIOUS, old)
        atomic_write(TRANSACTION, b'{"stage":"prepared"}\n')
    swapped = False
    try:
        populate(values)
        run(['/usr/sbin/ipset', 'swap', STAGE, SET])
        swapped = True
        if persist:
            atomic_write(CIDRS, data)
        # STAGE retains the entire previous live set until the commit finishes.
        run(['/usr/sbin/ipset', 'list', SET, '-terse'])
    except Exception:
        if swapped:
            run(['/usr/sbin/ipset', 'swap', STAGE, SET])
        if persist:
            atomic_write(CIDRS, old)
        run(['/usr/sbin/ipset', 'destroy', STAGE], check=False)
        if persist:
            TRANSACTION.unlink()
            PREVIOUS.unlink()
        raise
    # A failed rollback above leaves both the journal and old set for recovery.
    run(['/usr/sbin/ipset', 'destroy', STAGE], check=False)
    if persist:
        TRANSACTION.unlink()
        PREVIOUS.unlink()
    return data != old


def sync(config):
    ips = recent_ips(config)
    entries = ['127.0.0.0/8', '::1', *config['static_cidrs'], *ips]
    for entry in entries:
        ipaddress.ip_network(entry, strict=False)
    data = ('# Managed by pylon\n[sshd]\nignoreip = ' + ' '.join(dict.fromkeys(entries)) + '\n').encode()
    old = IGNORE.read_bytes() if IGNORE.exists() else None
    changed = data != old
    if changed:
        try:
            atomic_write(IGNORE, data)
            run(['/usr/bin/fail2ban-client', '-t'], timeout=60)
            run(['/usr/bin/fail2ban-client', 'reload', 'sshd'], timeout=60)
        except Exception:
            if old is None:
                IGNORE.unlink(missing_ok=True)
            else:
                atomic_write(IGNORE, old)
            run(['/usr/bin/fail2ban-client', 'reload', 'sshd'], check=False, timeout=60)
            raise
    for ip in ips:
        run(['/usr/bin/fail2ban-client', 'set', 'sshd', 'unbanip', ip])
    return {'changed': changed, 'trusted_ips': ips, 'entries': len(set(entries))}


def parse_archive(path):
    ranges = []
    with gzip.open(path, 'rt', encoding='utf-8', newline='') as stream:
        for row in csv.reader(stream):
            if len(row) < 6:
                raise ValueError('invalid DB-IP CSV row')
            if row[3].upper() != 'CN' or 'beijing' not in (row[4].casefold(), row[5].casefold()):
                continue
            start, end = (ipaddress.ip_address(v) for v in row[:2])
            if start.version == 4 and end.version == 4:
                ranges.extend(ipaddress.summarize_address_range(start, end))
    values = sorted(ipaddress.collapse_addresses(ranges), key=lambda n: (int(n.network_address), n.prefixlen))
    return networks('\n'.join(map(str, values)))


def refresh(config):
    month = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m')
    url = f'https://download.db-ip.com/free/dbip-city-lite-{month}.csv.gz'
    with tempfile.TemporaryDirectory(prefix='pylon-ssh-') as directory:
        archive = Path(directory) / 'city.csv.gz'
        run(['/usr/bin/curl', '-fsSL', '--retry', '3', '--retry-max-time', '700', '--connect-timeout', '20',
             '--max-time', '600', '-o', str(archive), url], timeout=740)
        values = parse_archive(archive)
        changed = install(values, protected=recent_ips(config))
    return {'changed': changed, 'cidrs': len(values), 'month': month}


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['status', 'check', 'sync', 'refresh', 'load'])
    parser.add_argument('address', nargs='?')
    options = parser.parse_args(args)
    if options.address and options.action != 'check':
        raise ValueError('unexpected argument')
    config = json.loads(CONFIG.read_text())
    with Path('/run/pylon-ssh.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if options.action in {'sync', 'refresh', 'load'}:
            recover()
        if options.action == 'load':
            values = networks(CIDRS.read_text())
            install(values, persist=False)
            result = {'cidrs': len(values), 'loaded': True}
        elif options.action == 'sync':
            result = sync(config)
        elif options.action == 'refresh':
            result = refresh(config)
        elif options.action == 'check':
            address = ipaddress.ip_address(options.address or '')
            ignored = IGNORE.read_text().split('ignoreip =', 1)[-1].split()
            result = {'address': str(address), 'beijing_set': member(address),
                      'fail2ban_exempt': any(address in ipaddress.ip_network(x, strict=False) for x in ignored)}
        else:
            values = networks(CIDRS.read_text())
            header = run(['/usr/sbin/ipset', 'list', SET, '-terse']).stdout
            count = re.search(r'Number of entries: (\d+)', header)
            result = {'cidrs': len(values), 'live_entries': int(count[1]) if count else None,
                      'trusted_ips': recent_ips(config), 'transaction_pending': TRANSACTION.exists()}
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
