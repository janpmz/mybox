"""Check the production rule generator without administrator access."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BOX = str(ROOT / ".build/mybox-firewall-manager")


class Firewall(unittest.TestCase):
    def setUp(self):
        self.policy = dict(owner=501, network="mybox-net-demo", subnet4="192.168.65.0/24",
                           subnet6="fd00:1234::/64", gateway="192.168.65.1", ports=[8080, 9000], active=True)

    def render(self, interfaces="bridge999,lo0,en0"):
        return subprocess.run([BOX, "render", json.dumps(self.policy), interfaces],
                              capture_output=True, text=True, timeout=10)

    def test_rules_cover_spoofed_sources_and_selected_ports(self):
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        rules = result.stdout
        incoming = [line for line in rules.splitlines() if " in quick " in line]
        self.assertTrue(all("on bridge999" in line for line in incoming))
        self.assertTrue(all("from any" in line or "inet6 all" in line for line in incoming))
        self.assertIn("(en0)", rules)
        self.assertIn("(en0:network)", rules)
        self.assertIn("port 8080", rules)
        self.assertIn("port 9000", rules)
        self.assertIn('label "mybox_unpublished"', rules)
        self.assertIn("block drop out quick inet6", rules)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pf") as file:
            file.write(rules)
            file.flush()
            syntax = subprocess.run(["/sbin/pfctl", "-nf", file.name], capture_output=True, text=True)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_rule_injection_and_unrelated_interfaces_are_rejected(self):
        for interfaces in ["en0,lo0", "lo0,en0", "bridge999,lo0\npass all", "bridge999,lo0;pass"]:
            with self.subTest(interfaces=interfaces):
                self.assertNotEqual(self.render(interfaces).returncode, 0)
        for key, value in [("network", "other"), ("subnet4", "8.8.8.0/24"),
                           ("gateway", "192.168.65.2"), ("ports", [0]),
                           ("ports", [8080, 8080]), ("subnet6", "2001:db8::/64"),
                           ("subnet6", "fd00::/64\npass all")]:
            previous = self.policy[key]
            self.policy[key] = value
            with self.subTest(key=key, value=value):
                self.assertNotEqual(self.render().returncode, 0)
            self.policy[key] = previous

    def test_no_selected_ports_means_no_new_host_connections(self):
        self.policy["ports"] = []
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("pass out", result.stdout)
        self.assertIn("mybox_unpublished", result.stdout)


if __name__ == "__main__":
    unittest.main()
