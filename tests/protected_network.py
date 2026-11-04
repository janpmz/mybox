"""Exercise the production helper/PF startup sequence on disposable boxes.

Run in a Mac terminal as the normal user. This installs the requested persistent
PF helper, then tests first workload, restart, spoofing, ports, and other boxes.
It leaves the installed firewall setup in place and removes all test fixtures.
--baseline-only changes no PF rules and does not install the firewall helper.
"""
import argparse
from datetime import datetime, timezone
import http.server
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import uuid

from dns import lookup
from network_isolation import (ROOT, PROBE, SERVER, IPv6HTTPServer, ProbeHandler,
                               container, command, check_internet, interfaces_for_gateway)
from prestart_network import host_http
from source_spoof import RAW

BOX = str(ROOT / ".build/mybox")
HELPER = "/Library/PrivilegedHelperTools/mybox-firewall-manager"
ANCHOR = "com.apple/000.mybox"

BOOT = r'''
use strict;
use warnings;
use IO::Socket::INET;
my ($host, $tcp, $udp, $marker) = @ARGV;
mkdir '/workspace';
open my $log, '>', '/workspace/startup.txt' or die $!;
open my $boot, '<', '/proc/sys/kernel/random/boot_id' or die $!;
print $log 'boot_id=' . <$boot>;
my $client = IO::Socket::INET->new(PeerAddr=>$host, PeerPort=>$tcp, Proto=>'tcp', Timeout=>3);
print $log 'host_connected=' . ($client ? 1 : 0) . "\n";
close $client if $client;
my $sent = eval { local @ARGV = ('198.18.0.2', $host, $udp, $marker); __RAW__; 1 };
print $log 'raw_sent=' . ($sent ? 1 : 0) . "\nraw_error=$@\ncomplete=1\n";
close $log;
sleep 3600 while 1;
'''.replace('__RAW__', RAW)


def box(*args, check=True):
    result = subprocess.run([BOX, *args], stdin=subprocess.DEVNULL, text=True,
                            stdout=subprocess.PIPE, stderr=None,
                            timeout=None if args == ('setup-network',) else 120)
    if check and result.returncode:
        raise RuntimeError('box failed: ' + repr(args) + '\n' + result.stdout)
    return result


def sudo(*args):
    result = subprocess.run(['/usr/bin/sudo', *args], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=60)
    if result.returncode:
        raise RuntimeError('Administrator command failed: ' + result.stderr)
    return result.stdout


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-only', action='store_true')
    parser.add_argument('--report', type=Path, default=ROOT / 'test-results/protected-network-latest.json')
    args = parser.parse_args()
    if os.geteuid() == 0:
        raise RuntimeError('Run this test as your normal Mac user, not with sudo.')
    suffix = uuid.uuid4().hex[:10]
    name = 'protected-' + suffix
    peer = 'peer-' + suffix
    baseline = 'baseline-' + suffix
    fixtures, networks, servers = [], [], []
    protected = set()
    received = []
    stop_udp = threading.Event()
    report = {'date': datetime.now(timezone.utc).isoformat(), 'baseline_only': args.baseline_only,
              'checks': {}, 'stages': {}, 'cleanup': {}, 'mac_reboot_tested': False,
              'physical_lan_tested': False}
    before_main = before_nat = None
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        report['existing_running_before'] = [x['id'] for x in json.loads(container('list', '--format', 'json').stdout)]
        route = command(['/sbin/route', '-n', 'get', 'default']).stdout
        interface = re.search(r'interface: ([a-z][a-z0-9]+)', route).group(1)
        host4 = re.search(r'\binet (\d+\.\d+\.\d+\.\d+)\b', command(['/sbin/ifconfig', interface]).stdout).group(1)
        host_server = http.server.ThreadingHTTPServer((host4, 0), ProbeHandler)
        servers.append(host_server)
        threading.Thread(target=host_server.serve_forever, daemon=True).start()
        udp.bind((host4, 0))
        udp.settimeout(.2)

        def listen():
            while not stop_udp.is_set():
                try:
                    data, source = udp.recvfrom(4096)
                    received.append({'data': data.decode(errors='replace'), 'source': list(source)})
                except socket.timeout:
                    pass

        thread = threading.Thread(target=listen, daemon=True)
        thread.start()

        def create_fixture(short, boot=True):
            network = 'mybox-net-' + short
            identifier = 'mybox-' + short
            port = free_port()
            container('network', 'create', '--label', 'com.mybox.managed=true', network)
            networks.append(network)
            command_args = ['create', '--name', identifier, '--label', 'com.mybox.managed=true',
                            '--label', 'com.mybox.network=' + network, '--network', network + ',alias=' + short + '.box.test',
                            '--cpus', '1', '--memory', '256m', '--dns', '1.1.1.1', '--dns', '1.0.0.1',
                            '--publish', f'127.0.0.1:{port}:8080', '--entrypoint', '/usr/bin/perl',
                            'debian:trixie-slim', '-e', BOOT, host4, str(host_server.server_port), str(udp.getsockname()[1]), short]
            container(*command_args)
            fixtures.append(short)
            return port

        def probe(short, host, port, family='4', phase_timeout=3):
            result = container('exec', 'mybox-' + short, '/usr/bin/perl', '-e', PROBE, host, str(port), family, '1',
                               str(phase_timeout), check=False, timeout=phase_timeout * 3 + 5)
            return {'connected': result.returncode == 0, 'output': result.stdout.strip(), 'error': result.stderr.strip()}

        def observe(short, outside, stage, blocked):
            identifier = 'mybox-' + short
            boot_id = container('exec', identifier, '/bin/cat', '/proc/sys/kernel/random/boot_id').stdout.strip()
            deadline = time.monotonic() + 15
            while True:
                startup = container('exec', identifier, '/bin/cat', '/workspace/startup.txt', check=False).stdout
                if 'complete=1' in startup and 'boot_id=' + boot_id in startup:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('The first workload did not finish: ' + startup)
                time.sleep(.2)
            for port in (8080, 8081):
                container('exec', '--detach', identifier, '/usr/bin/perl', '-e', SERVER, str(port))
            metadata = json.loads(container('inspect', identifier).stdout)[0]
            guest = metadata['status']['networks'][0]
            guest4 = guest['ipv4Address'].split('/')[0]
            expected_host = 'host_connected=' + ('0' if blocked else '1')
            raw_arrived = any(item['data'] == short for item in received)
            values = {'startup': startup, 'guest': guest, 'raw_packets': list(received),
                      'internet': check_internet(lambda host, port, **kw: probe(short, host, port, **kw)),
                      'host_after_start': probe(short, host4, host_server.server_port),
                      'selected': host_http(guest4, 8080), 'hostname': host_http(short + '.box.test', 8080),
                      'unpublished': host_http(guest4, 8081), 'localhost': host_http('127.0.0.1', outside)}
            checks = {'first_workload_blocked' if blocked else 'baseline_host_reachable': expected_host in startup,
                      'raw_socket_available': 'raw_sent=1' in startup,
                      'raw_spoof_policy': raw_arrived != blocked,
                      'host_policy_after_start': values['host_after_start']['connected'] != blocked,
                      'internet_dns': values['internet']['connected'],
                      'selected_port': values['selected'], 'hostname_port': values['hostname'],
                      'unpublished_port': values['unpublished'] != blocked, 'localhost': values['localhost'],
                      'dns_current': lookup(short + '.box.test') == [guest4],
                      'both_guest_servers_running': all(probe(short, '127.0.0.1', port)['connected'] for port in (8080, 8081))}
            report['stages'][stage] = values
            report['checks'][stage] = checks
            print(stage + ': ' + json.dumps(checks), flush=True)
            if not all(checks.values()):
                raise RuntimeError(stage + ' failed: ' + repr([k for k, v in checks.items() if not v]))
            return guest

        # Prove the listeners and raw spoof probe before enabling the new policy.
        port = create_fixture(baseline)
        trusted = 'mybox-helper-' + str(uuid.uuid4())
        container('create', '--name', trusted, '--network', 'mybox-net-' + baseline,
                  '--label', 'com.mybox.helper=true', '--memory', '256m', '--cpus', '1', '--read-only',
                  '--entrypoint', '/bin/sleep', 'debian:trixie-slim', 'infinity')
        report['baseline_helper'] = trusted
        try:
            container('start', trusted)
            container('start', 'mybox-' + baseline)
        finally:
            container('stop', trusted, check=False)
            if container('delete', trusted, check=False).returncode:
                raise RuntimeError('Could not remove the baseline helper.')
        observe(baseline, port, 'baseline', False)
        container('stop', 'mybox-' + baseline)
        container('delete', 'mybox-' + baseline)
        fixtures.remove(baseline)
        container('network', 'delete', 'mybox-net-' + baseline)
        networks.remove('mybox-net-' + baseline)
        if args.baseline_only:
            report['success'] = True
            return

        print('Baseline passed. Installing the requested PF setup requires administrator authentication.', flush=True)
        box('setup-network')
        report['persistent_setup_installed'] = True
        before_main = sudo('/sbin/pfctl', '-sr')
        before_nat = sudo('/sbin/pfctl', '-sn')
        port = create_fixture(name)
        protected.add(name)
        for stage in ('first_start', 'restart'):
            received.clear()
            box('start', name)
            guest = observe(name, port, stage, True)
            active_helpers = [x['id'] for x in json.loads(container('list', '--all', '--format', 'json').stdout)
                              if x['configuration'].get('labels', {}).get('com.mybox.helper') == 'true'
                              and any(n['network'] == 'mybox-net-' + name for n in x['configuration'].get('networks', []))]
            report['checks'][stage]['helper_removed'] = not active_helpers
            if active_helpers:
                raise RuntimeError('The production helper was not removed.')
            if stage == 'first_start':
                box('stop', name)
        report['checks']['different_boot_ids'] = report['stages']['first_start']['startup'].splitlines()[0] != report['stages']['restart']['startup'].splitlines()[0]

        # Use the actual CLI create path for the second isolated box.
        peer_port = free_port()
        fixtures.append(peer)
        networks.append('mybox-net-' + peer)
        protected.add(peer)
        box('create', peer, '--inside-port', '8080', '--outside-port', str(peer_port), '--memory', '256m', '--cpus', '1')
        container('exec', '--detach', 'mybox-' + peer, '/usr/bin/perl', '-e', SERVER, '8080')
        peer_guest = json.loads(container('inspect', 'mybox-' + peer).stdout)[0]['status']['networks'][0]
        guest4 = guest['ipv4Address'].split('/')[0]
        peer4 = peer_guest['ipv4Address'].split('/')[0]
        report['checks']['other_boxes'] = {
            'peer_server_running': probe(peer, '127.0.0.1', 8080)['connected'],
            'first_to_peer_blocked': not probe(name, peer4, 8080)['connected'],
            'peer_to_first_blocked': not probe(peer, guest4, 8080)['connected'],
            'host_selected_peer': host_http('127.0.0.1', peer_port),
        }
        _, host6 = interfaces_for_gateway(guest['ipv4Gateway'])
        http6 = IPv6HTTPServer((host6, 0), ProbeHandler)
        servers.append(http6)
        threading.Thread(target=http6.serve_forever, daemon=True).start()
        report['checks']['host_ipv6_blocked'] = not probe(name, host6, http6.server_port, family='6')['connected']
        # Exercise the boot restore entry point without restarting the Mac.
        sudo(HELPER, 'restore')
        box('start', name)
        report['checks']['restore_entry_point'] = not probe(name, host4, host_server.server_port)['connected'] and host_http('127.0.0.1', port)
        report['firewall_counters'] = sudo('/sbin/pfctl', '-a', ANCHOR, '-vvsr')
        report['checks']['main_rules_preserved'] = before_main == sudo('/sbin/pfctl', '-sr')
        report['checks']['main_nat_preserved'] = before_nat == sudo('/sbin/pfctl', '-sn')
        report['checks']['startup_plist_valid'] = command(['/usr/bin/plutil', '-lint', '/Library/LaunchDaemons/com.mybox.firewall.plist'], check=False).returncode == 0

        def passed(value):
            return all(passed(item) for item in value.values()) if isinstance(value, dict) else value is True
        report['success'] = passed(report['checks'])
        if not report['success']:
            raise RuntimeError('A protected-network check failed.')
    except BaseException as error:
        report['success'] = False
        report['error'] = repr(error)
        raise
    finally:
        for short in reversed(fixtures):
            if short in protected:
                stopped = box('stop', short, check=False).returncode == 0
            else:
                stopped = container('stop', 'mybox-' + short, check=False).returncode == 0
            deleted = container('delete', 'mybox-' + short, check=False).returncode == 0
            report['cleanup'][short] = stopped and deleted
        for network in reversed(networks):
            report['cleanup'][network] = container('network', 'delete', network, check=False).returncode == 0
        for server in servers:
            server.shutdown()
            server.server_close()
        stop_udp.set()
        if 'thread' in locals():
            thread.join(timeout=2)
        udp.close()
        report['existing_running_after'] = [x['id'] for x in json.loads(container('list', '--format', 'json').stdout)]
        report['success'] = report.get('success', False) and all(report['cleanup'].values()) and report.get('existing_running_before') == report['existing_running_after']
        args.report.write_text(json.dumps(report, indent=2) + '\n')
        print('Results: ' + str(args.report), flush=True)
    if not report['success']:
        raise RuntimeError('Cleanup failed. Check the test report.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print('Protected network test: ' + str(error), file=sys.stderr)
        sys.exit(1)
