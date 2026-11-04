"""Prevent false passes when destinations or connection probes fail."""
import json
import subprocess
import unittest
from unittest.mock import patch

import reboot_network as network


class NetworkChecks(unittest.TestCase):
    def outcomes(self, outcome):
        return {key: {"outcome": outcome} for key in ("mac_address", "mac_gateway", "mac_ipv6", "lan_endpoint")}

    def test_blocked_connections_and_working_internet_pass(self):
        checks = network.isolation_checks(self.outcomes("connected"), self.outcomes("timeout"),
                                          self.outcomes("connected"), {"connected": True})
        self.assertTrue(all(checks.values()))

    def test_unreachable_control_destinations_fail(self):
        for stage in ("before", "after"):
            for key in self.outcomes("connected"):
                with self.subTest(stage=stage, destination=key):
                    before, after = self.outcomes("connected"), self.outcomes("connected")
                    (before if stage == "before" else after)[key]["outcome"] = "timeout"
                    checks = network.isolation_checks(before, self.outcomes("timeout"), after, {"connected": True})
                    self.assertFalse(checks["destinations_reachable_" + stage])

    def test_reachable_destination_or_probe_error_never_counts_as_blocked(self):
        for outcome in ("connected", "error"):
            with self.subTest(outcome=outcome):
                checks = network.isolation_checks(self.outcomes("connected"), self.outcomes(outcome),
                                                  self.outcomes("connected"), {"connected": True})
                for key in ("mac_address_blocked", "mac_gateway_blocked", "mac_ipv6_blocked", "lan_endpoint_blocked"):
                    self.assertFalse(checks[key])

    def test_internet_failure_and_missing_results_fail(self):
        checks = network.isolation_checks(self.outcomes("connected"), self.outcomes("timeout"),
                                          self.outcomes("connected"), {"connected": False})
        self.assertFalse(checks["internet_and_dns_work"])
        self.assertFalse(any(network.isolation_checks({}, {}, {}, {}).values()))
        self.assertFalse(network.passed({"success": True, "checks": {}}))
        report = {"success": True, "checks": dict.fromkeys(network.REQUIRED_CHECKS, True)}
        self.assertTrue(network.passed(report))
        report["checks"]["control_network_deleted"] = False
        self.assertFalse(network.passed(report))

    def test_probe_requires_exact_exit_status_and_result(self):
        for code, output, expected in [(0, "MYBOX_TCP connected\n", "connected"),
                                       (10, "MYBOX_TCP timeout\n", "timeout"),
                                       (1, "", "error"), (127, "MYBOX_TCP timeout\n", "error"),
                                       (0, "", "error")]:
            with self.subTest(code=code, output=output):
                result = subprocess.CompletedProcess([], code, output, "details")
                with patch.object(network, "run", return_value=result):
                    self.assertEqual(network.probe("test", "192.168.1.1", 80)["outcome"], expected)

    def test_ipv6_waits_for_duplicate_address_detection(self):
        for flags, ready in [("c0", False), ("88", False), ("80", True)]:
            output = f"fe800000000000000000000000000001 02 40 20 {flags} eth0\n"
            with self.subTest(flags=flags), patch.object(network, "run", return_value=subprocess.CompletedProcess([], 0, output, "")):
                self.assertEqual(network.ipv6_ready("test"), ready)

    def test_router_selection_requires_a_successful_connection(self):
        attempts = []
        configuration = {"address": "192.168.1.1", "ports": [443, 80, 53]}
        with patch.object(network, "probe", side_effect=[{"outcome": "error"}, {"outcome": "connected"}]):
            self.assertEqual(network.select_endpoint("test", configuration, attempts), ("192.168.1.1", 80))
        self.assertEqual(len(attempts), 2)
        with patch.object(network, "probe", return_value={"outcome": "timeout"}):
            with self.assertRaisesRegex(RuntimeError, "No tested LAN port"):
                network.select_endpoint("test", configuration, [])

    def test_lan_selection_rejects_mac_public_and_invalid_targets(self):
        route = subprocess.CompletedProcess([], 0, " gateway: 192.168.1.1\n interface: en0\n", "")
        config = subprocess.CompletedProcess([], 0, "inet 192.168.1.10 netmask 0xffffff00", "")
        with patch.object(network, "run", side_effect=[route, config]):
            selected = network.lan_configuration()
        self.assertEqual(selected["address"], "192.168.1.1")
        self.assertEqual(selected["mac_address"], "192.168.1.10")
        with patch.object(network, "run", side_effect=[route, config]):
            self.assertEqual(network.lan_configuration("192.168.1.20:8080")["ports"], [8080])
        for target in ("192.168.1.10:80", "1.1.1.1:443", "192.168.1.255:80", "192.168.1.0:80"):
            with self.subTest(target=target), patch.object(network, "run", side_effect=[route, config]):
                with self.assertRaisesRegex(RuntimeError, "separate device"):
                    network.lan_configuration(target)
        for target in ("bad", "host:80", "192.168.1.1:0", "192.168.1.1:65536", "192.168.1.1:80; command"):
            with self.subTest(target=target), patch.object(network, "run") as run:
                with self.assertRaises(RuntimeError):
                    network.lan_configuration(target)
                run.assert_not_called()

    def test_failed_probe_cleans_up_control_resources(self):
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            output = json.dumps([{"status": {"networks": [{"ipv4Gateway": "192.168.65.1"}]}}]) if args[1] == "inspect" else ""
            return subprocess.CompletedProcess(args, 0, output, "")

        report = {}
        with patch.object(network, "lan_configuration", return_value={"mac_address": "192.168.1.10"}), \
             patch.object(network, "run", side_effect=fake_run), \
             patch.object(network.socketserver, "ThreadingTCPServer") as server, \
             patch.object(network, "IPv6Listener") as server6, \
             patch.object(network, "host_ipv6", return_value="fe80::1"), \
             patch.object(network, "ipv6_ready", return_value=True), \
             patch.object(network.threading, "Thread"), \
             patch.object(network, "select_endpoint", return_value=("192.168.1.1", 80)), \
             patch.object(network, "probe", return_value={"outcome": "error"}):
            server.return_value.server_address = ("0.0.0.0", 12345)
            server6.return_value.server_address = ("::", 12346)
            with self.assertRaisesRegex(RuntimeError, "Blocking is not proven"):
                network.verify("mybox-test", report)
            server.return_value.server_close.assert_called_once()
            server6.return_value.server_close.assert_called_once()
        self.assertFalse(report["success"])
        self.assertTrue(report["cleanup"]["control_container_deleted"])
        self.assertTrue(report["cleanup"]["control_network_deleted"])
        self.assertIn(("container", "delete", report["control_id"]), calls)
        self.assertIn(("container", "network", "delete", report["control_network"]), calls)
        self.assertFalse(any("setup-network" in args for args in calls))


if __name__ == "__main__":
    unittest.main()
