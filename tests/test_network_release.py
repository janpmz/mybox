"""Verify firewall ordering around the fork's on-demand network lifecycle."""
import ipaddress
import json
import unittest
import test_cli


class NetworkRelease(unittest.TestCase):
    run_box = test_cli.CLI.run_box
    calls = test_cli.CLI.calls

    def setUp(self):
        test_cli.CLI.setUp(self)
        bridge = self.directory / 'bridge'
        bridge.write_text('''#!/usr/bin/python3
import json, os, sys
def log(action):
    with open(os.environ['MYBOX_TEST_LOG'], 'a') as file:
        file.write(json.dumps(['bridge', action, sys.argv[1]]) + '\\n')
log('prepare')
print('ready', flush=True)
sys.stdin.buffer.read(1)
log('release')
sys.exit(int(os.environ.get('MYBOX_TEST_RELEASE_FAILURE', '0')))
''')
        bridge.chmod(0o755)
        self.environment['MYBOX_TEST_BRIDGE'] = str(bridge)

    def test_start_checks_firewall_before_linux_and_retains_bridge_until_started(self):
        self.environment['MYBOX_TEST_SCENARIO'] = 'stopped'
        self.run_box('start', 'demo')
        calls = self.calls()
        prepare = calls.index(['bridge', 'prepare', 'mybox-net-demo'])
        start = calls.index(['start', 'mybox-demo'])
        release = calls.index(['bridge', 'release', 'mybox-net-demo'])
        checks = [i for i, call in enumerate(calls) if call[:2] == ['host', '/usr/bin/sudo']]
        self.assertLess(prepare, checks[0])
        self.assertLess(checks[1], start)
        self.assertLess(start, release)
        self.assertLess(release, checks[-1])
        self.assertFalse(any(call[0] == 'create' for call in calls))

    def test_stop_removes_firewall_before_releasing_bridge(self):
        self.run_box('stop', 'demo')
        calls = self.calls()
        stop = calls.index(['stop', 'mybox-demo'])
        remove = next(i for i, call in enumerate(calls) if call[:2] == ['host', '/usr/bin/sudo'] and call[3] == 'remove')
        self.assertLess(calls.index(['bridge', 'prepare', 'mybox-net-demo']), stop)
        self.assertLess(stop, remove)
        self.assertLess(remove, calls.index(['bridge', 'release', 'mybox-net-demo']))

    def test_stopping_stopped_box_does_not_create_bridge(self):
        self.environment['MYBOX_TEST_SCENARIO'] = 'stopped'
        self.run_box('stop', 'demo')
        self.assertFalse(any(call[0] == 'bridge' for call in self.calls()))

    def test_failed_firewall_releases_preparation_without_starting_linux(self):
        self.environment.update(MYBOX_TEST_SCENARIO='stopped', MYBOX_TEST_FIREWALL_FAIL='apply')
        self.run_box('start', 'demo', success=False)
        self.assertNotIn(['start', 'mybox-demo'], self.calls())
        self.assertIn(['bridge', 'release', 'mybox-net-demo'], self.calls())

    def test_failed_stop_preserves_firewall(self):
        self.environment['MYBOX_TEST_BACKEND_FAIL'] = json.dumps(['stop'])
        self.run_box('stop', 'demo', success=False)
        self.assertFalse(any(call[:2] == ['host', '/usr/bin/sudo'] and call[3] == 'remove' for call in self.calls()))

    def test_failed_cleanup_is_reported(self):
        self.environment['MYBOX_TEST_RELEASE_FAILURE'] = '1'
        result = self.run_box('stop', 'demo', success=False)
        self.assertIn('could not confirm cleanup', result.stderr)

    def test_new_network_uses_saved_private_addresses_and_on_demand_variant(self):
        self.run_box('create', 'other')
        call = next(call for call in self.calls() if call[:2] == ['network', 'create'])
        self.assertEqual(call[call.index('--option') + 1], 'variant=reserved-on-demand')
        subnet = ipaddress.ip_network(call[call.index('--subnet') + 1])
        self.assertTrue(subnet.is_private)
        self.assertFalse(subnet.overlaps(ipaddress.ip_network('192.168.65.0/24')))
        self.assertEqual(subnet.prefixlen, 24)
        self.assertEqual(ipaddress.ip_network(call[call.index('--subnet-v6') + 1]).prefixlen, 64)
