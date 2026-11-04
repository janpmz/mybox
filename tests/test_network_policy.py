"""Offline checks for the temporary PF experiment; never invokes real pfctl."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import network_isolation as network


class NetworkPolicy(unittest.TestCase):
    def configuration(self):
        return {"anchor": "com.apple/000.mybox-net-test-012345abcdef",
                "interfaces": ["bridge101", "vmenet1"], "subnet4": "192.168.65.0/24",
                "host_interfaces": ["lo0", "en0", "bridge100", "bridge101"],
                "subnet6": "fd85:e9d0:b9d8:1869::/64", "inside_port": 8080}

    def test_refuses_physical_and_loopback_interfaces_or_injection(self):
        for interface in ["en0", "lo0", "utun0", "vmenet1\npass all", "bridge101; pass all"]:
            configuration = self.configuration()
            configuration["interfaces"] = [interface]
            with self.subTest(interface=interface), self.assertRaises(ValueError):
                network.rules_for(configuration)

    def test_refuses_public_subnets_and_invalid_ports(self):
        for field, value in [("subnet4", "8.8.8.0/24"), ("subnet6", "2606:4700::/64"),
                             ("inside_port", 0), ("inside_port", 65536),
                             ("inside_port", "8080; pass all")]:
            configuration = self.configuration()
            configuration[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                network.rules_for(configuration)

    def test_prestart_policy_needs_no_guest_interface_and_restricts_both_directions(self):
        configuration = self.configuration()
        configuration.pop("interfaces")
        configuration["policy"] = "prestart-v4"
        configuration["host_interfaces"] = ["lo0", "en0"]
        rules = network.rules_for(configuration)
        self.assertIn("from 192.168.65.1 to 192.168.65.0/24 port 8080", rules)
        self.assertIn("block drop out quick inet to 192.168.65.0/24", rules)
        self.assertIn("block drop in quick inet from 192.168.65.0/24", rules)
        self.assertNotIn("on bridge", rules)
        for field, value in [("subnet4", "127.0.0.0/24"), ("subnet4", "10.0.0.0/8"),
                             ("subnet6", "::/64"), ("policy", "unexpected")]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                network.rules_for(dict(configuration, **{field: value}))

    def run_fake_firewall(self, fail_counters=False, fail_flush=False):
        temporary = tempfile.TemporaryDirectory(prefix="mybox-pf-unit-")
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        configuration = self.configuration()
        (directory / "configuration.json").write_text(json.dumps(configuration))
        calls = []

        def fake_command(arguments, **kwargs):
            calls.append(arguments)
            if arguments == [network.PFCTL, "-sr"]:
                return subprocess.CompletedProcess(arguments, 0, 'anchor "com.apple/*" all\n', "")
            if arguments == [network.PFCTL, "-E"]:
                return subprocess.CompletedProcess(arguments, 0, "", "Token : 12345\n")
            if "-f" in arguments:
                (directory / "done").touch()
            if "-vvsr" in arguments and fail_counters:
                raise RuntimeError("simulated counter read failure")
            if arguments[-2:] == ["-F", "rules"] and fail_flush:
                return subprocess.CompletedProcess(arguments, 1, "", "simulated flush failure")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch.object(network.os, "geteuid", return_value=0), patch.object(network, "command", side_effect=fake_command):
            if fail_counters:
                with self.assertRaisesRegex(RuntimeError, "counter read failure"):
                    network.root_firewall(directory)
            else:
                network.root_firewall(directory)
        return directory, configuration, calls

    def test_cleanup_is_scoped_and_releases_only_its_own_pf_reference(self):
        directory, configuration, calls = self.run_fake_firewall()
        self.assertIn([network.PFCTL, "-a", configuration["anchor"], "-F", "rules"], calls)
        self.assertIn([network.PFCTL, "-X", "12345"], calls)
        self.assertNotIn([network.PFCTL, "-d"], calls)
        self.assertNotIn([network.PFCTL, "-f", "/etc/pf.conf"], calls)
        self.assertTrue((directory / "cleaned").exists())
        result = json.loads((directory / "firewall-result.json").read_text())
        self.assertTrue(result["main_rules_unchanged"])
        self.assertTrue(result["main_nat_unchanged"])

    def test_cleanup_also_runs_when_measurement_fails(self):
        directory, configuration, calls = self.run_fake_firewall(fail_counters=True)
        self.assertIn([network.PFCTL, "-a", configuration["anchor"], "-F", "rules"], calls)
        self.assertIn([network.PFCTL, "-X", "12345"], calls)
        self.assertTrue((directory / "cleaned").exists())

    def test_reference_release_is_attempted_even_if_rule_cleanup_fails(self):
        directory, _, calls = self.run_fake_firewall(fail_flush=True)
        self.assertIn([network.PFCTL, "-X", "12345"], calls)
        result = json.loads((directory / "firewall-result.json").read_text())
        self.assertEqual(result["cleanup_errors"], ["simulated flush failure"])

    def test_finished_test_cannot_apply_stale_rules(self):
        with tempfile.TemporaryDirectory(prefix="mybox-ended-unit-") as temporary:
            directory = Path(temporary)
            (directory / "done").touch()
            with patch.object(network.os, "geteuid", return_value=0), patch.object(network, "command") as backend:
                with self.assertRaisesRegex(RuntimeError, "test has ended"):
                    network.root_firewall(directory)
            backend.assert_not_called()

    def test_internet_retry_preserves_failed_attempt_and_success(self):
        failed = {"connected": False, "output": "", "error": "DNS lookup: timeout"}
        connected = {"connected": True, "output": "HTTP/1.1 200 OK", "error": ""}
        with patch.object(network.time, "sleep") as pause:
            probe = unittest.mock.Mock(side_effect=[failed, connected])
            result = network.check_internet(probe)
        self.assertTrue(result["connected"])
        self.assertEqual(result["attempts"], [failed, connected])
        self.assertEqual(probe.call_count, 2)
        pause.assert_called_once()

    def test_persistent_internet_failure_still_fails_the_baseline(self):
        failed = {"connected": False, "output": "", "error": "TCP connection: timeout"}
        with patch.object(network.time, "sleep"):
            probe = unittest.mock.Mock(return_value=failed)
            result = network.check_internet(probe)
        self.assertFalse(result["connected"])
        self.assertEqual(probe.call_count, 2)
        with self.assertRaisesRegex(RuntimeError, "internet_ipv4: TCP connection: timeout"):
            network.require_baseline({"host_ipv4": {"connected": True}, "internet_ipv4": result, "localhost": True})


if __name__ == "__main__":
    unittest.main(verbosity=2)
