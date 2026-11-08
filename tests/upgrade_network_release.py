"""Install tested on-demand network helpers and migrate existing MyBox networks.

Run with macOS administrator authorization, SUDO_UID set to the installing
account, and --restart-box for each running private box approved for restart.
Without --apply this only displays the planned migration. No box disks change.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid

INSTALL = Path('/Library/PrivilegedHelperTools/io.github.janpmz.my-container-box.service')

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = Path('/usr/local/libexec/container/plugins/container-network-vmnet/bin/container-network-vmnet')
BRIDGE = Path('/usr/local/libexec/container/container-mybox-bridge')
WORKER = INSTALL / 'mybox-worker'


def current_file(path, content, mode):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
        raise ValueError(f'Unexpected ownership or file type at {path}.')
    return stat.S_IMODE(info.st_mode) == mode and path.read_bytes() == content


def store(path, content, mode):
    if current_file(path, content, mode):
        return False
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        temp = Path(out.name)
        try:
            out.write(content)
            out.flush()
            os.fchmod(out.fileno(), mode)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return True


def run(*args, check=True, timeout=120):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{args!r}: {result.stderr or result.stdout}')
    return result


def runtime(uid, *args, **kwargs):
    return run('/bin/launchctl', 'asuser', str(uid), '/usr/bin/sudo', '-n', '-H', '-u', f'#{uid}', '--',
               '/usr/local/bin/container', *args, **kwargs)


def records(uid):
    return json.loads(runtime(uid, 'list', '--all', '--format', 'json').stdout)


def bridges():
    found, name = {}, None
    for line in run('/sbin/ifconfig', '-a').stdout.splitlines():
        if line and not line[0].isspace():
            name = line.split(':', 1)[0]
        fields = line.split()
        if fields[:1] == ['inet'] and re.fullmatch(r'bridge\d+', name or ''):
            found[fields[1]] = name
    return found


def worker(uid, owner, action, box):
    if action not in {'start', 'stop'} or not re.fullmatch('[a-z][a-z0-9-]{0,47}', box):
        raise ValueError('Maintenance only supports starting and stopping named boxes.')
    with tempfile.TemporaryFile() as output:
        # Only the existing root firewall manager grants firewall approval.
        # This channel never grants entry or executes a command inside a box.
        local, remote = socket.socketpair()
        with local, remote:
            local.settimeout(1)
            process = subprocess.Popen(['/bin/launchctl', 'asuser', str(uid), '/usr/bin/sudo', '-n', '-H', '-u', f'#{uid}', '--',
                '/usr/bin/env', 'MYBOX_FIREWALL_CHANNEL=0', str(WORKER), action, box],
                stdin=remote, stdout=output, stderr=output)
            remote.close()
            deadline, pending = time.monotonic() + 180, bytearray()
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Maintenance worker timed out.')
                    try:
                        chunk = local.recv(36 - len(pending))
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    pending.extend(chunk)
                    if len(pending) != 36:
                        continue
                    token = pending.decode('ascii')
                    if str(uuid.UUID(token)) != token:
                        raise ValueError('Invalid firewall request token.')
                    approved = subprocess.run(['/Library/PrivilegedHelperTools/mybox-firewall-manager', 'approve', token],
                        stdout=output, stderr=output, timeout=60, env=dict(os.environ, SUDO_UID=str(owner)))
                    local.sendall(b'Y' if approved.returncode == 0 else b'N')
                    pending.clear()
                code = process.wait(timeout=15)
                output.seek(0)
                print(output.read().decode(errors='replace'), end='', flush=True)
                if code:
                    raise RuntimeError(f'{action} {box} failed ({code}).')
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)


def replace_metadata(path, content, uid, gid, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        pending = Path(out.name)
        try:
            out.write(content)
            out.flush()
            os.fchown(out.fileno(), uid, gid)
            os.fchmod(out.fileno(), mode)
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restart-box', action='append', default=[])
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    owner = int(os.environ.get('SUDO_UID', '0'))
    private = pwd.getpwnam('_mybox').pw_uid
    if os.geteuid() != 0 or owner <= 0 or owner != int((INSTALL / 'owner').read_text()):
        raise PermissionError('Administrator authorization from the installing owner is required.')
    allowed = {'mybox-' + box for box in args.restart_box}
    evidence = json.loads((ROOT / 'test-results/network-release-latest.json').read_text())
    sources = {PLUGIN: ROOT / '.build/network-release/container-network-vmnet',
               BRIDGE: ROOT / '.build/network-release/container-mybox-bridge', WORKER: ROOT / '.build/mybox-worker'}
    if not evidence['passed'] or not evidence['existing_bridges_preserved']:
        raise ValueError('The live network release test has not passed.')
    for destination, field in [(PLUGIN, 'plugin_sha256'), (BRIDGE, 'helper_sha256')]:
        source = sources[destination]
        if hashlib.sha256(source.read_bytes()).hexdigest() != evidence[field]:
            raise ValueError('The candidate differs from the tested binary.')
        run('/usr/bin/codesign', '--verify', '--strict', str(source))
    for destination, source in sources.items():
        info = destination.lstat()
        if not source.is_file() or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f'Unexpected binary ownership or type: {destination}')

    accounts, updates, occupied = [], [], []
    for uid in (owner, private):
        current = records(uid)
        running = {item['id'] for item in current if item['status']['state'] != 'stopped'}
        if (uid == owner and running) or (uid == private and running - allowed):
            raise ValueError(f'Unapproved running containers in account {uid}: {sorted(running)}')
        status = json.loads(runtime(uid, 'system', 'status', '--format', 'json').stdout)
        root = Path(status['paths']['appRoot']).resolve()
        expected = Path(pwd.getpwuid(uid).pw_dir) / 'Library/Application Support/com.apple.container'
        if root != expected.resolve():
            raise ValueError(f'Unexpected runtime storage: {root}')
        networks = json.loads(runtime(uid, 'network', 'list', '--format', 'json').stdout)
        accounts.append(dict(uid=uid, root=root, running=running))
        for network in networks:
            occupied.append(ipaddress.ip_network(network['status']['ipv4Subnet'], strict=False))
            config = network['configuration']
            labels = config.get('labels', {})
            if not (labels.get('io.github.janpmz.my-container-box.managed') == 'true' or labels.get('com.mybox.managed') == 'true'):
                continue
            name = network['id']
            if not re.fullmatch('mybox-net-[a-z][a-z0-9-]{0,47}', name) or config['mode'] != 'nat':
                raise ValueError(f'Unexpected managed network: {name}')
            if config.get('options', {}).get('variant') == 'reserved-on-demand':
                continue
            path = root / 'networks' / name / 'entity.json'
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != uid:
                raise ValueError(f'Unexpected saved network ownership: {path}')
            original = path.read_bytes()
            saved = json.loads(original)
            if saved['name'] != name:
                raise ValueError('Network metadata does not match its directory.')
            updates.append(dict(uid=uid, path=path, original=original, saved=saved,
                gid=info.st_gid, mode=stat.S_IMODE(info.st_mode), old_gateway=network['status']['ipv4Gateway']))

    # Fresh ranges avoid reservations abandoned by the old helper. They remain
    # saved settings while stopped; the new helper reserves them only on use.
    for line in run('/sbin/ifconfig', '-a').stdout.splitlines():
        words = line.split()
        if words[:1] == ['inet'] and 'netmask' in words:
            mask = int(words[words.index('netmask') + 1], 16)
            occupied.append(ipaddress.ip_network(f'{words[1]}/{bin(mask).count("1")}', strict=False))
    for update in updates:
        for attempt in range(10000):
            subnet = ipaddress.ip_network(f'10.{secrets.randbelow(256)}.{secrets.randbelow(256)}.0/24')
            if not any(subnet.overlaps(other) for other in occupied):
                break
        else:
            raise ValueError('No unused migration subnet found.')
        occupied.append(subnet)
        saved = update['saved']
        saved['ipv4Subnet'] = str(subnet)
        h = secrets.token_hex(7)
        saved['ipv6Subnet'] = f'fd{h[:2]}:{h[2:6]}:{h[6:10]}:{h[10:14]}::/64'
        saved.setdefault('options', {})['variant'] = 'reserved-on-demand'
        update['gateway'] = str(subnet.network_address + 1)
    restart = sorted(name.removeprefix('mybox-') for account in accounts if account['uid'] == private for name in account['running'])
    plan = dict(restart_boxes=restart, networks=[dict(owner=item['uid'], network=item['saved']['name'],
        ipv4_subnet=item['saved']['ipv4Subnet'], ipv6_subnet=item['saved']['ipv6Subnet']) for item in updates])
    print(json.dumps(plan, indent=2), flush=True)
    if not args.apply:
        return
    backup = Path('/var/db/mybox') / ('network-release-backup-' + uuid.uuid4().hex)
    backup.mkdir(mode=0o700)
    artifacts = {path: path.read_bytes() for path in sources}
    for index, (path, data) in enumerate(artifacts.items()):
        store(backup / f'binary-{index}', data, 0o700)
    for index, update in enumerate(updates):
        store(backup / f'network-{index}.json', update['original'], 0o600)
    store(backup / 'manifest.json', json.dumps(dict(plan=plan, binaries=[str(p) for p in sources],
        metadata=[str(item['path']) for item in updates]), indent=2).encode(), 0o600)
    try:
        for name in restart:
            worker(private, owner, 'stop', name)
    except BaseException:
        for name in restart:
            worker(private, owner, 'start', name)
        raise
    mutation_started = False
    try:
        with ExitStack() as stack:
            for account in accounts:
                home = Path(pwd.getpwuid(account['uid']).pw_dir)
                for kind in ('network', 'service'):
                    path = home / '.config/mybox' / (kind + '.lock')
                    # Existing installations already created these directories.
                    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                    os.fchown(fd, account['uid'], pwd.getpwuid(account['uid']).pw_gid)
                    lock = stack.enter_context(os.fdopen(fd, 'r+b'))
                    fcntl.flock(lock, fcntl.LOCK_EX)
                if any(item['status']['state'] != 'stopped' for item in records(account['uid'])):
                    raise ValueError('Another container started during migration.')
            for account in accounts:
                mutation_started = True
                runtime(account['uid'], 'system', 'stop')
            for destination, source in sources.items():
                store(destination, source.read_bytes(), 0o755)
            for item in updates:
                replace_metadata(item['path'], json.dumps(item['saved']).encode(), item['uid'], item['gid'], item['mode'])
            for account in accounts:
                runtime(account['uid'], 'system', 'start', '--disable-kernel-install')
            for item in updates:
                actual = json.loads(runtime(item['uid'], 'network', 'inspect', item['saved']['name']).stdout)[0]
                if actual['status']['ipv4Gateway'] != item['gateway'] or item['gateway'] in bridges():
                    raise RuntimeError('A migrated idle network unexpectedly retained a bridge or changed address.')
    except BaseException:
        if not mutation_started:
            for name in restart:
                worker(private, owner, 'start', name)
            raise
        for account in accounts:
            status = runtime(account['uid'], 'system', 'status', '--format', 'json', check=False)
            if status.returncode == 0 and any(item['status']['state'] != 'stopped' for item in records(account['uid'])):
                raise RuntimeError('A container started during maintenance; automatic rollback was refused. Backups: ' + str(backup))
            runtime(account['uid'], 'system', 'stop', check=False)
        for destination, data in artifacts.items():
            store(destination, data, 0o755)
        for item in updates:
            replace_metadata(item['path'], item['original'], item['uid'], item['gid'], item['mode'])
        for account in accounts:
            runtime(account['uid'], 'system', 'start', '--disable-kernel-install', check=False)
        for name in restart:
            worker(private, owner, 'start', name)
        raise
    for name in restart:
        worker(private, owner, 'start', name)
    report = dict(installed_at=datetime.now(timezone.utc).isoformat(), backup=str(backup), plan=plan,
        installed_sha256={str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources},
        idle_networks_checked=True, restarted=restart, passed=True)
    output = ROOT / 'test-results/network-release-install-latest.json'
    output.write_text(json.dumps(report, indent=2) + '\n')
    os.chown(output, owner, pwd.getpwuid(owner).pw_gid)
    print('Installed on-demand networking and restarted the approved boxes.', flush=True)


if __name__ == '__main__':
    main()
