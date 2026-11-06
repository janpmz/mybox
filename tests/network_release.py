"""Live release/recreation checks using disposable networks in the login account.

Uses cached Debian and supplied signed binaries; installs nothing globally.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import select
import secrets
import subprocess
import sys
import uuid

from network_lifetime import ROOT, apple, bridges, command, use_plugin, wait_bridge


def main():
    plugin, helper = [Path(path).resolve(strict=True) for path in sys.argv[1:]]
    for path in (plugin, helper):
        command('/usr/bin/codesign', '--verify', '--strict', str(path))
    apple('image', 'inspect', 'docker.io/library/debian:trixie-slim')
    prefix = 'mybox-release-' + uuid.uuid4().hex[:10]
    subnet_group = secrets.randbelow(200) + 20
    ipv6_group = secrets.token_hex(2)
    baseline = bridges()
    networks, boxes, checks, fixtures = [], [], [], []
    report = dict(plugin_sha256=hashlib.sha256(plugin.read_bytes()).hexdigest(),
                  helper_sha256=hashlib.sha256(helper.read_bytes()).hexdigest(), passed=False, checks=checks)

    def checked(label):
        actual = bridges()
        assert all(actual.get(gateway) == bridge for gateway, bridge in baseline.items()), actual
        checks.append(label)
        print('Passed:', label, flush=True)

    @contextmanager
    def prepared(fixture, kill=False):
        proc = subprocess.Popen([str(helper), fixture['network']], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            assert select.select([proc.stdout], [], [], 20)[0], 'Preparation timeout'
            assert proc.stdout.readline() == b'ready\n', 'Preparation failed'
            yield wait_bridge(fixture['gateway'], True)
        finally:
            if kill:
                proc.kill()
            proc.stdin.close()
            try:
                code = proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise
            proc.stdout.close()
            if not kill:
                assert code == 0, code

    def online(fixture):
        apple('exec', fixture['box'], '/usr/bin/timeout', '8', '/bin/bash', '-c', ': > /dev/tcp/1.1.1.1/443')

    try:
        for index in range(12):
            network = f'{prefix}-{index}'
            # Bootstrap through the installed allocation-only variant, which
            # creates no live reservation. Then load the candidate helper.
            apple('network', 'create', '--option', 'variant=allocationOnly',
                  '--subnet', f'10.{subnet_group}.{index}.0/24', network)
            networks.append(network)
            state = json.loads(apple('network', 'inspect', network))[0]['status']
            state['ipv6Subnet'] = f'fd57:ac4b:{ipv6_group}:{index:x}::/64'
            use_plugin(network, plugin, state, on_demand=True)
            fixture = dict(network=network, box=network + '-vm', gateway=state['ipv4Gateway'])
            fixtures.append(fixture)
            wait_bridge(fixture['gateway'], False)
            apple('create', '--name', fixture['box'], '--network', network, '--read-only',
                  '--cpus', '1', '--memory', '256m', '--entrypoint', '/bin/sleep',
                  'docker.io/library/debian:trixie-slim', 'infinity')
            boxes.append(fixture['box'])
        assert len(bridges()) == len(baseline)
        checked('12 stopped boxes retain zero bridges; existing networks are unchanged')
        a, b = fixtures[:2]
        with prepared(a, kill=True):
            pass
        wait_bridge(a['gateway'], False)
        checked('abandoned preparation releases its bridge without starting a guest')
        with prepared(a) as first:
            with prepared(a) as second:
                assert first == second
            assert wait_bridge(a['gateway'], True) == first
        wait_bridge(a['gateway'], False)
        checked('overlapping preparation clients retain the bridge until both finish')
        with prepared(a, kill=True):
            command('/bin/launchctl', 'kill', 'SIGTERM',
                    f'gui/{os.getuid()}/com.apple.container.container-network-vmnet.' + a['network'])
            wait_bridge(a['gateway'], False)
        # A new helper must reconstruct settings without retaining a reservation
        # or inheriting the previous helper's network object.
        with prepared(a):
            pass
        wait_bridge(a['gateway'], False)
        checked('helper termination releases the reservation; its replacement can recreate it')
        with prepared(a) as bridge_a:
            apple('start', a['box'])
        assert wait_bridge(a['gateway'], True) == bridge_a
        for cycle in range(3):
            with prepared(b) as bridge_b:
                assert bridge_b != bridge_a
                apple('start', b['box'])
            online(a)
            online(b)
            with prepared(b):
                apple('stop', b['box'])
                assert wait_bridge(b['gateway'], True) == bridge_b
            wait_bridge(b['gateway'], False)
            assert bridges()[a['gateway']] == bridge_a
            online(a)
            checked(f'cycle {cycle + 1}: b recreates and releases its bridge; a remains online')
        with prepared(b) as bridge_b:
            apple('start', b['box'])
        # No preparation client at stop: the VM allocation's disconnect must
        # perform cleanup even when the MyBox worker is absent.
        apple('stop', a['box'])
        wait_bridge(a['gateway'], False)
        online(b)
        with prepared(a) as bridge_a:
            assert bridge_a != bridge_b
            apple('start', a['box'])
        online(a)
        online(b)
        checked('automatic cleanup and recreation of a preserve b and its bridge')
        apple('stop', a['box'])
        apple('stop', b['box'])
        for fixture in fixtures:
            wait_bridge(fixture['gateway'], False)
        assert len(bridges()) == len(baseline)
        checked('all 12 boxes stopped again; no box bridges or bridge leaks remain')
        report['passed'] = True
    finally:
        for box in reversed(boxes):
            apple('delete', '--force', box)
        for network in reversed(networks):
            apple('network', 'delete', network)
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        report['existing_bridges_preserved'] = all(bridges().get(g) == b for g, b in baseline.items())
        (ROOT / 'test-results/network-release-latest.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
