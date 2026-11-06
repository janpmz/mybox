"""Check that stopping and restarting one disposable network cannot disconnect another.

The supplied signed network plugin is loaded only for two new test networks in
the calling account. Existing boxes, network helpers, and PF rules are not changed.
Uses the cached Debian image and its existing Bash; installs no dependencies.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile
import time
import uuid

APPLE = '/usr/local/bin/container'
ROOT = Path(__file__).resolve().parents[1]


def command(*args, check=True, timeout=90):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{args!r}: {result.stderr or result.stdout}')
    return result


def apple(*args, **kwargs):
    return command(APPLE, *args, **kwargs).stdout


def bridges():
    result, current = {}, None
    for line in command('/sbin/ifconfig', '-a').stdout.splitlines():
        if line and not line[0].isspace():
            current = line.split(':', 1)[0]
        fields = line.split()
        if fields[:1] == ['inet'] and re.fullmatch(r'bridge\d+', current or ''):
            result[fields[1]] = current
    return result


def wait_bridge(gateway, present):
    for _ in range(100):
        found = bridges().get(gateway)
        if bool(found) == present:
            return found
        time.sleep(.05)
    raise RuntimeError(f'Unexpected bridge state for {gateway}: {bridges()}')


def use_plugin(network, plugin, state, *, on_demand=False):
    root = Path(json.loads(apple('system', 'status', '--format', 'json'))['paths']['appRoot'])
    original = root / 'networks' / network / 'service.plist'
    job = plistlib.loads(original.read_bytes())
    if not job['Label'].endswith('.' + network):
        raise RuntimeError('Unexpected test network helper label.')
    domain = f'gui/{os.getuid()}'
    label = domain + '/' + job['Label']
    job['ProgramArguments'][0] = str(plugin)
    job['ProgramArguments'] += ['--subnet', state['ipv4Subnet'], '--subnet-v6', state['ipv6Subnet']]
    if on_demand:
        if '--variant' in job['ProgramArguments']:
            index = job['ProgramArguments'].index('--variant')
            del job['ProgramArguments'][index:index + 2]
        job['ProgramArguments'] += ['--variant', 'reserved-on-demand']
    with tempfile.NamedTemporaryFile(suffix='.plist') as temporary:
        temporary.write(plistlib.dumps(job))
        temporary.flush()
        command('/bin/launchctl', 'bootout', label)
        try:
            command('/bin/launchctl', 'bootstrap', domain, temporary.name)
            apple('network', 'inspect', network)
        except BaseException:
            command('/bin/launchctl', 'bootout', label, check=False)
            command('/bin/launchctl', 'bootstrap', domain, str(original))
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('network_plugin', type=Path)
    parser.add_argument('--cycles', type=int, default=3)
    options = parser.parse_args()
    plugin = options.network_plugin.resolve(strict=True)
    command('/usr/bin/codesign', '--verify', '--strict', str(plugin))
    # Fetching an image is deliberately not part of this test.
    apple('image', 'inspect', 'docker.io/library/debian:trixie-slim')
    prefix = 'mybox-lifetime-' + uuid.uuid4().hex[:10]
    fixtures, created_boxes, created_networks, checks = [], [], [], []
    baseline = bridges()

    def check(label):
        actual = bridges()
        for gateway, bridge in baseline.items():
            if actual.get(gateway) != bridge:
                raise RuntimeError(f'An existing bridge changed: {gateway}, {bridge}, {actual}')
        for fixture in fixtures:
            if actual.get(fixture['gateway']) != fixture['bridge']:
                raise RuntimeError(f'Test network lost its bridge: {fixture}, {actual}')
        if len({fixture['bridge'] for fixture in fixtures}) != len(fixtures):
            raise RuntimeError('Two test networks share a bridge.')
        checks.append(label)
        print('Passed:', label, flush=True)

    def online(fixture):
        apple('exec', fixture['box'], '/usr/bin/timeout', '8', '/bin/bash', '-c',
              ': > /dev/tcp/1.1.1.1/443')

    try:
        for suffix in ('a', 'b'):
            network = prefix + '-' + suffix
            apple('network', 'create', network)
            created_networks.append(network)
            state = json.loads(apple('network', 'inspect', network))[0]['status']
            use_plugin(network, plugin, state)
            gateway = state['ipv4Gateway']
            fixture = dict(network=network, box=network + '-vm', gateway=gateway,
                           bridge=wait_bridge(gateway, True))
            fixtures.append(fixture)
            apple('create', '--name', fixture['box'], '--network', network, '--read-only',
                  '--cpus', '1', '--memory', '256m', '--entrypoint', '/bin/sleep',
                  'docker.io/library/debian:trixie-slim', 'infinity')
            created_boxes.append(fixture['box'])
            check(f'{suffix}: bridge exists before any guest starts')
        first, second = fixtures
        apple('start', second['box'])
        online(second)
        apple('stop', second['box'])
        check('b: bridge survives its last guest stopping')
        apple('start', first['box'])
        online(first)
        for cycle in range(options.cycles):
            apple('start', second['box'])
            online(first)
            online(second)
            check(f'cycle {cycle + 1}: both boxes retain separate working networks')
            apple('stop', second['box'])
            online(first)
            check(f'cycle {cycle + 1}: stopping b leaves a online')
        apple('start', second['box'])
        apple('stop', first['box'])
        online(second)
        check('stopping a leaves b online')
        apple('start', first['box'])
        online(first)
        online(second)
        check('restarting a leaves b online')
    finally:
        for box in reversed(created_boxes):
            command(APPLE, 'delete', '--force', box)
        for network in reversed(created_networks):
            apple('network', 'delete', network)
        for fixture in fixtures:
            wait_bridge(fixture['gateway'], False)
        actual = bridges()
        if any(actual.get(gateway) != name for gateway, name in baseline.items()):
            raise RuntimeError('An existing bridge changed during test cleanup.')
    checks.append('test networks deleted; existing bridges preserved')
    report = dict(finished_at=datetime.now(timezone.utc).isoformat(), plugin=str(plugin),
                  cycles=options.cycles, fixtures=fixtures, checks=checks, passed=True)
    (ROOT / 'test-results/network-lifetime-latest.json').write_text(json.dumps(report, indent=2) + '\n')
    print('Passed all network lifetime checks.', flush=True)


if __name__ == '__main__':
    main()
