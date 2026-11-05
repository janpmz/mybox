"""Run the firewall lifecycle against fake host tools. No real PF changes occur."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / ".build/mybox-firewall-test"
BASE_INTERFACES = """lo0: flags=0
    inet 127.0.0.1 netmask 0xff000000
en0: flags=0
    inet 192.168.1.10 netmask 0xffffff00
bridge999: flags=0
    inet 192.168.65.1 netmask 0xffffff00
    member: vmenet999 flags=0
"""
NEW_INTERFACE = """en7: flags=0
    inet 11.22.33.44 netmask 0xffffff00
"""

FAKE_HOST = r'''#!/usr/bin/python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ['MYBOX_TEST_FIREWALL_DIRECTORY'])
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as log:
    log.write(json.dumps(args) + '\n')
failure = root / 'fail-once'
if args[0] == '/sbin/ifconfig':
    print((root / 'interfaces.txt').read_text())
elif args[0] == '/usr/sbin/netstat':
    print('Destination Gateway Flags Netif Expire')
    routes = root / (args[-1] + '.routes')
    if routes.exists(): print(routes.read_text())
elif args[0] == '/usr/sbin/sysctl':
    print('boot-test')
elif args[0] == '/sbin/pfctl':
    if args[1:] == ['-sr']:
        print('anchor "com.apple/*" all')
    elif args[1:] == ['-a', 'com.apple', '-s', 'Anchors']:
        print('000.mybox')
    elif args[1:] == ['-s', 'References']:
        print('Token : 12345')
    elif args[1:] == ['-E']:
        print('Token : 12345')
    elif args[1:] == ['-si']:
        print('Status: Enabled')
    elif '-nf' in args:
        pass
    elif '-f' in args:
        rules = pathlib.Path(args[-1]).read_text()
        mode = failure.read_text() if failure.exists() else ''
        if mode == 'load' and 'mybox_suspended' not in rules:
            failure.unlink()
            sys.exit('simulated rule load failure')
        (root / 'active.pf').write_text(rules)
    elif args[-1] == '-sr':
        print((root / 'active.pf').read_text() if (root / 'active.pf').exists() else '', end='')
    elif '-k' in args:
        if failure.exists() and failure.read_text() == 'states':
            failure.unlink()
            sys.exit('simulated state cleanup failure')
    else:
        sys.exit('unexpected PF command: ' + repr(args))
else:
    sys.exit('unexpected host command: ' + repr(args))
'''


class FirewallRefresh(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mybox-refresh-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        host = self.root / "host"
        host.write_text(FAKE_HOST)
        host.chmod(0o755)
        self.env = dict(os.environ, MYBOX_TEST_FIREWALL_DIRECTORY=str(self.root), MYBOX_TEST_HOST=str(host), SUDO_UID=str(os.getuid()))
        self.policy = dict(owner=os.getuid(), network="mybox-net-demo", subnet4="192.168.65.0/24",
                           subnet6="fd00:1234::/64", gateway="192.168.65.1", ports=[8080], active=True)
        (self.root / "interfaces.txt").write_text(BASE_INTERFACES)
        self.run_manager("apply", json.dumps(self.policy))
        self.reset_calls()

    def run_manager(self, *args, success=True):
        result = subprocess.run([MANAGER, *args], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def test_new_ipv6_lan_and_vpn_routes_revoke_existing_states(self):
        self.change_interfaces(BASE_INTERFACES.replace("en0: flags=0", "en0: flags=0\n    inet6 2001:db8:8::a prefixlen 64"))
        (self.root / "inet6.routes").write_text(
            "default fe80::1%utun3 UGcIg utun3\n2001:db8:6::/56 fe80::1 UGSc en0\n"
            "2001:db8:7::/64 link#44 USc utun3\n")
        self.run_manager("refresh")
        rules = (self.root / "policy.pf").read_text()
        for subnet in ["2001:db8:6::/56", "2001:db8:7::/64"]:
            self.assertIn(subnet, rules)
            self.assertIn(["/sbin/pfctl", "-i", "bridge999", "-k", "::/0", "-k", subnet], self.calls())
        for destination in ["2001:db8:8::a/128", "2001:db8:8::/64"]:
            self.assertIn(["/sbin/pfctl", "-i", "bridge999", "-k", "::/0", "-k", destination], self.calls())
        self.assertNotIn("::/0", rules)
        self.assertLess(rules.index("mybox_routed"), rules.index("mybox_internet6"))

    def test_vpn_internet_routes_preserve_more_specific_private_destinations(self):
        vpn_interface = ("utun7: flags=0\n"
                         "    inet 10.8.0.2 --> 10.8.0.1 netmask 0x0\n"
                         "    inet6 fd00:7::2 prefixlen 0\n"
                         "    inet6 2001:db8:77::1 prefixlen 64\n")
        self.change_interfaces(BASE_INTERFACES + vpn_interface)
        v4_routes = self.root / "inet.routes"
        v6_routes = self.root / "inet6.routes"
        for four, six in [
            (["default", "0.0.0.0/0"], ["default", "::/0"]),
            (["0/1", "128/1"], ["::/1", "8000::/1"]),
            (["default"], ["2000::/3"]),
            (["default"], ["2000::/4", "3000::/4"]),
        ]:
            with self.subTest(four=four, six=six):
                v4_routes.write_text("".join(f"{dest} 10.8.0.1 UGSc utun7\n" for dest in four + ["10.42/16", "11.22.33/24"]))
                v6_routes.write_text("".join(f"{dest} link#44 USc utun7\n" for dest in six + ["2001:db8:7::/64"]))
                self.run_manager("refresh")
                snapshot = json.loads((self.root / "snapshot.json").read_text())
                self.assertEqual(set(snapshot["routed"]), {"10.42.0.0/16", "11.22.33.0/24", "2001:db8:7::/64", "2001:db8:77::/64"})
                rules = (self.root / "policy.pf").read_text()
                self.assertIn("(utun7)", rules)
                self.assertNotIn("(utun7:network)", rules)
                self.assertLess(rules.index("mybox_routed"), rules.index("mybox_internet6"))

        self.change_interfaces(BASE_INTERFACES)
        v4_routes.write_text("")
        v6_routes.write_text("")
        self.run_manager("refresh")
        self.assertEqual(json.loads((self.root / "snapshot.json").read_text())["routed"], [])
        self.assertIn("mybox_internet6", (self.root / "policy.pf").read_text())

    def reset_calls(self):
        (self.root / "calls.jsonl").write_text("")

    def calls(self):
        return [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]

    def change_interfaces(self, value=BASE_INTERFACES + NEW_INTERFACE):
        path = self.root / "new-interfaces.txt"
        path.write_text(value)
        path.replace(self.root / "interfaces.txt")

    def test_verify_refreshes_new_interface_without_restart(self):
        self.change_interfaces()
        self.run_manager("verify", json.dumps(self.policy))
        self.assertIn("(en7)", (self.root / "active.pf").read_text())
        calls = self.calls()
        for destination in ["11.22.33.44/32", "11.22.33.0/24"]:
            self.assertIn(["/sbin/pfctl", "-i", "bridge999", "-k", "0.0.0.0/0", "-k", destination], calls)
        self.assertFalse(any("-F" in call or "-d" in call for call in calls))
        load = next(i for i, call in enumerate(calls) if call[0] == "/sbin/pfctl" and "-f" in call)
        cleanup = next(i for i, call in enumerate(calls) if "-k" in call)
        self.assertLess(load, cleanup)
        self.assertFalse(any("container" in arg for call in calls for arg in call))

    def test_address_and_netmask_changes_clear_newly_blocked_states(self):
        self.change_interfaces(BASE_INTERFACES.replace("192.168.1.10 netmask 0xffffff00", "11.22.33.44 netmask 0xffff0000"))
        self.run_manager("refresh")
        self.assertIn(["/sbin/pfctl", "-i", "bridge999", "-k", "0.0.0.0/0", "-k", "11.22.0.0/16"], self.calls())

    def test_no_change_does_not_reload_rules_or_interrupt_connections(self):
        self.run_manager("refresh")
        self.assertFalse(any(call[0] == "/sbin/pfctl" and ("-f" in call or "-k" in call) for call in self.calls()))

    def test_adding_ports_preserves_all_existing_connections(self):
        expanded = dict(self.policy, ports=[8080, 8081])
        self.run_manager("update-ports", json.dumps(expanded))
        self.assertIn("port 8081", (self.root / "active.pf").read_text())
        self.assertFalse(any("-k" in call for call in self.calls()))
        self.run_manager("verify", json.dumps(expanded))

    def test_removing_ports_clears_only_mac_to_box_connections(self):
        self.run_manager("update-ports", json.dumps(dict(self.policy, ports=[])))
        self.assertNotIn("mybox_selected", (self.root / "active.pf").read_text())
        kills = [call for call in self.calls() if "-k" in call]
        self.assertEqual(kills, [["/sbin/pfctl", "-k", "192.168.65.1", "-k", "192.168.65.0/24"]])
        self.assertEqual(json.loads((self.root / "state.json").read_text())["pendingHostCleanup"], [])

    def test_live_port_update_cannot_change_network_identity(self):
        changed = dict(self.policy, subnet4="192.168.66.0/24", gateway="192.168.66.1")
        self.run_manager("update-ports", json.dumps(changed), success=False)
        self.assertFalse(any(call[0] == "/sbin/pfctl" and ("-f" in call or "-k" in call) for call in self.calls()))

    def test_failed_port_revocation_is_retried_by_the_watcher(self):
        (self.root / "fail-once").write_text("states")
        self.run_manager("update-ports", json.dumps(dict(self.policy, ports=[])), success=False)
        self.assertTrue(json.loads((self.root / "state.json").read_text())["pendingHostCleanup"])
        self.run_manager("refresh")
        self.assertEqual(json.loads((self.root / "state.json").read_text())["pendingHostCleanup"], [])

    def test_helper_membership_does_not_interrupt_connections(self):
        self.change_interfaces(BASE_INTERFACES + "    member: vmenet998 flags=0\nvmenet998: flags=0\n")
        self.run_manager("refresh")
        self.assertFalse(any(call[0] == "/sbin/pfctl" and ("-f" in call or "-k" in call) for call in self.calls()))

    def test_removed_interface_refreshes_without_killing_unaffected_states(self):
        self.change_interfaces()
        self.run_manager("refresh")
        self.reset_calls()
        self.change_interfaces(BASE_INTERFACES)
        self.run_manager("refresh")
        self.assertNotIn("(en7)", (self.root / "active.pf").read_text())
        self.assertFalse(any("-k" in call for call in self.calls()))

    def test_refresh_failure_blocks_network_and_recovers_automatically(self):
        for failure in ["load", "states"]:
            with self.subTest(failure=failure):
                self.change_interfaces()
                (self.root / "fail-once").write_text(failure)
                # Force a changed snapshot on each iteration.
                (self.root / "snapshot.json").unlink()
                self.run_manager("refresh", success=False)
                blocked = (self.root / "active.pf").read_text()
                self.assertIn('block drop quick on bridge999 all label "mybox_suspended"', blocked)
                self.assertNotIn("pass ", blocked)
                self.assertTrue(json.loads((self.root / "snapshot.json").read_text())["blocked"])
                self.reset_calls()
                self.run_manager("refresh")
                self.assertIn("mybox_internet", (self.root / "active.pf").read_text())
                self.assertFalse(json.loads((self.root / "snapshot.json").read_text())["blocked"])
                calls = self.calls()
                cleanup = next(i for i, call in enumerate(calls) if "-k" in call)
                load = next(i for i, call in enumerate(calls) if call[0] == "/sbin/pfctl" and "-f" in call)
                self.assertLess(cleanup, load, "Recovery must clear old states before allowing traffic.")

    def test_missing_bridge_never_reuses_an_old_bridge_number(self):
        self.change_interfaces(BASE_INTERFACES.replace("192.168.65.1", "192.168.66.1"))
        self.run_manager("refresh")
        self.assertNotIn("bridge999", (self.root / "active.pf").read_text())
        self.assertFalse(any("-k" in call for call in self.calls()))

    def test_launch_job_keeps_the_watcher_running(self):
        config = json.loads(self.run_manager("_test-launch-config").stdout)
        self.assertEqual(config["ProgramArguments"][-1], "watch")
        self.assertTrue(config["KeepAlive"])
        self.assertTrue(config["RunAtLoad"])

    def test_periodic_check_catches_a_missed_notification_without_cli_commands(self):
        process = subprocess.Popen([MANAGER, "watch"], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 12
            while not (self.root / "watcher.json").exists() and time.monotonic() < deadline:
                self.assertIsNone(process.poll())
                time.sleep(.05)
            self.assertTrue((self.root / "watcher.json").exists())
            self.change_interfaces()
            while time.monotonic() < deadline:
                self.assertIsNone(process.poll())
                if "(en7)" in (self.root / "active.pf").read_text():
                    break
                time.sleep(.05)
            self.assertIn("(en7)", (self.root / "active.pf").read_text())
        finally:
            process.terminate()
            process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
