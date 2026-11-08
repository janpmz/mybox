"""Check the installed private account with one disposable box from the owner's terminal."""
import argparse
import array
import errno
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import pwd
import re
import stat
import socket
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
import service_protocol as broker

BOX = str(ROOT / 'mybox')
RUNTIME = '/Library/Application Support/MyBox/Runtime/bin/container'


def run(*args, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=240)
    if check and result.returncode:
        raise RuntimeError(result.stderr or result.stdout or f'{args[0]} failed.')
    return result


def rejected_start(name, delay_approval=False):
    # Test an explicitly malicious client. A true reply is insufficient; even
    # a genuine approval must not authorize a start at an arbitrary later time.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn, tempfile.TemporaryFile() as data:
        conn.settimeout(180)
        conn.connect(broker.SOCKET)
        broker.packet(conn, ['start', name])
        with open(os.devnull, 'rb') as empty, tempfile.TemporaryFile() as out:
            fds = array.array('i', [empty.fileno(), out.fileno(), out.fileno(), data.fileno()])
            conn.sendmsg([b'F'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])
            request = broker.receive(conn)
            if request[0] != 'firewall':
                raise RuntimeError('The private service did not request a firewall check.')
            if delay_approval:
                token = str(uuid.UUID(request[1]))
                run('/usr/bin/sudo', broker.FIREWALL, 'approve', token)
                time.sleep(6)
            broker.packet(conn, ['firewall', True])
            result = broker.receive(conn)
            if result[0] != 'exit' or result[1] == 0:
                raise RuntimeError('The service accepted a forged or expired firewall confirmation.')
            out.seek(0)
            expected = b'confirmation expired' if delay_approval else b'did not confirm this request'
            if expected not in out.read():
                raise RuntimeError('Startup failed for an unexpected reason.')


def check_reboot():
    stamp = run('/usr/sbin/sysctl', '-n', 'kern.boottime').stdout
    match = re.search(r'\bsec\s*=\s*(\d+)', stamp)
    if not match:
        raise RuntimeError('Cannot read the Mac startup time.')
    boot = int(match[1])
    bootstrap = Path('/Library/LaunchDaemons/io.github.janpmz.my-container-box.bootstrap.plist')
    if bootstrap.stat().st_mtime >= boot:
        raise RuntimeError('Restart the Mac after setup before running the reboot check.')
    endpoint = Path(broker.SOCKET).lstat()
    if not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_ctime < boot:
        raise RuntimeError('The private service did not create its socket during this boot.')
    print('The private service started during this boot. Checking a new box.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--after-reboot', action='store_true')
    options = parser.parse_args()
    account = pwd.getpwnam('_mybox')
    if os.getuid() in (0, account.pw_uid) or not sys.stdin.isatty():
        raise RuntimeError('Run make service-test from your normal Mac terminal.')
    if options.after_reboot:
        check_reboot()
        # The direct-access control below also needs a working personal fork service.
        # Starting that service cannot start the private broker or any test workload.
        if run(RUNTIME, 'system', 'status', '--format', 'json', check=False).returncode:
            run(RUNTIME, 'system', 'start', '--disable-kernel-install')
    # Confirm the normal account has a working fork service. A broken CLI is
    # insufficient evidence that it cannot reach the private service.
    run(RUNTIME, 'list', '--all', '--format', 'json')
    run(BOX, 'list')
    name = 'access-test-' + uuid.uuid4().hex[:12]
    cid = 'mybox-' + name
    network = 'mybox-net-' + name
    print('Testing one disposable box:', name, flush=True)
    # Only cleanup uses administrator access to Apple's private test fixtures.
    runtime = ['/usr/bin/sudo', '/bin/launchctl', 'asuser', str(account.pw_uid),
               '/usr/bin/sudo', '-H', '-u', '_mybox', '--', RUNTIME]
    created = False
    try:
        run(BOX, 'create', name)
        created = True
        assert run(BOX, 'enter', name, '--', '/bin/pwd').stdout.strip() == '/workspace'
        if sys.stdout.isatty():
            subprocess.run([BOX, 'enter', name, '--', '/bin/sh', '-c', 'test -t 0 && test -t 1'],
                           check=True, timeout=120)
        assert name in run(BOX, 'list').stdout
        assert cid not in run(RUNTIME, 'list', '--all', '--format', 'json').stdout
        assert run(RUNTIME, 'exec', cid, '/bin/true', check=False).returncode != 0
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'sample'
            source.write_bytes(b'MyBox file transfer\0')
            run(BOX, 'import', name, str(source))
            dest = Path(tmp) / 'exported'
            run(BOX, 'export', name, 'sample', str(dest))
            assert dest.read_bytes() == source.read_bytes()
        run(BOX, 'stop', name)
        assert run(RUNTIME, 'start', cid, check=False).returncode != 0
        disk = Path(account.pw_dir) / 'Library/Application Support/com.apple.container/containers' / cid / 'rootfs.ext4'
        try:
            with disk.open('rb'):
                raise RuntimeError('The owner account could read the private disk.')
        except PermissionError as error:
            assert error.errno in (errno.EACCES, errno.EPERM)
        rejected_start(name)
        rejected_start(name, delay_approval=True)
        row = next(line for line in run(BOX, 'list').stdout.splitlines() if line.startswith(name + ' '))
        assert 'stopped' in row
        run(BOX, 'start', name)
        assert run(BOX, 'enter', name, '--', '/bin/cat', '/workspace/sample').stdout == 'MyBox file transfer\0'
    finally:
        # A failed create can still leave a stopped box or a dedicated network.
        if created or run(*runtime, 'inspect', cid, check=False).returncode == 0:
            run(BOX, 'stop', name)
            run(*runtime, 'delete', cid)
        if run(*runtime, 'network', 'inspect', network, check=False).returncode == 0:
            run(*runtime, 'network', 'delete', network)
    report = {'finished_at': datetime.now(timezone.utc).isoformat(), 'box': name, 'service_uid': account.pw_uid,
              'backend': run(RUNTIME, '--version').stdout.strip(), 'after_reboot': options.after_reboot,
              'terminal_checked': sys.stdout.isatty(),
              'passed': ['private shell', 'direct exec denied', 'direct start denied', 'stopped disk private',
                         'file transfer', 'forged firewall approval rejected', 'expired firewall approval rejected',
                         'protected restart', 'cleanup']}
    results = ROOT / 'test-results'
    filename = 'private-service-reboot-latest.json' if options.after_reboot else 'private-service-latest.json'
    with tempfile.NamedTemporaryFile(mode='w', dir=results, delete=False) as output:
        json.dump(report, output, indent=2)
        output.write('\n')
    os.replace(output.name, results / filename)
    print('Passed private shell access, direct access denial, file transfer, stopped disk privacy, and firewall checks.')


if __name__ == '__main__':
    main()
