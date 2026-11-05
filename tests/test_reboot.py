"""Check that reboot verification cannot accept old startup evidence."""
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import reboot
from reboot import require_new_boot, startup_checks


class RebootChecks(unittest.TestCase):
    def values(self):
        return dict(boot={"uuid": "new", "time": "{ sec = 1000, usec = 1 }"},
                    job="state = running\npid = 42\n", status={"pid": 42, "lastCheck": 1010},
                    store={"boot": "{ sec = 1000, usec = 1 }", "token": "123"},
                    snapshot={"boot": "{ sec = 1000, usec = 1 }", "blocked": False},
                    pf_info="Status: Enabled", references="Token : 123\n", now=1012)

    def test_same_boot_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "has not restarted"):
            require_new_boot({"uuid": "same"}, {"uuid": "same"})
        require_new_boot({"uuid": "old"}, {"uuid": "new"})

    def test_current_healthy_startup_passes(self):
        self.assertTrue(all(startup_checks(**self.values()).values()))

    def test_stale_status_and_old_policy_fail(self):
        values = self.values()
        values["status"] = {"pid": 41, "lastCheck": 990}
        values["store"]["boot"] = "{ sec = 900, usec = 0 }"
        checks = startup_checks(**values)
        self.assertFalse(checks["watcher_running_automatically"])
        self.assertFalse(checks["watcher_report_is_current"])
        self.assertFalse(checks["saved_policy_uses_current_boot"])

    def test_errors_suspension_disabled_pf_and_wrong_token_fail(self):
        values = self.values()
        values["status"]["error"] = "refresh failed"
        values["snapshot"]["blocked"] = True
        values["pf_info"] = "Status: Disabled"
        values["references"] = "Token : 1234\n"
        checks = startup_checks(**values)
        for key in ["watcher_reports_no_error", "networking_is_not_suspended", "pf_enabled_before_box_commands", "mybox_pf_reference_is_active"]:
            self.assertFalse(checks[key])


class RebootWorkflowChecks(unittest.TestCase):
    def verify_workflow(self, changed_kind=None, service_running=False, workflow_timeout=False, network_passed=True):
        initial = {"filter": 'anchor "com.apple/*" all\n', "nat": 'nat-anchor "com.apple/*" all\n'}
        ready = {"filter": initial["filter"] + 'anchor "com.apple.internet-sharing" all\n',
                 "nat": initial["nat"] + 'nat-anchor "com.apple.internet-sharing" all\n'}
        final = dict(ready)
        if changed_kind:
            final[changed_kind] = 'unexpected rule\n' + final[changed_kind]
        active = ready if service_running else initial
        events = []
        boot = {"uuid": "new", "time": "{ sec = 1000, usec = 1 }"}
        status = {"pid": 42, "lastCheck": 1010}

        def result(stdout="", returncode=0):
            return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")

        def fake_sudo(*args, **kwargs):
            events.append(args)
            responses = {
                ("/bin/launchctl", "print", "system/io.github.janpmz.my-container-box.firewall"): "state = running\npid = 42\n",
                ("/bin/cat", "/var/db/mybox/watcher.json"): json.dumps(status),
                ("/bin/cat", "/var/db/mybox/state.json"): json.dumps({"boot": boot["time"], "token": "123"}),
                ("/bin/cat", "/var/db/mybox/snapshot.json"): json.dumps({"boot": boot["time"], "blocked": False}),
                ("/sbin/pfctl", "-si"): "Status: Enabled",
                ("/sbin/pfctl", "-s", "References"): "Token : 123\n",
                ("/sbin/pfctl", "-sr"): active["filter"],
                ("/sbin/pfctl", "-sn"): active["nat"],
            }
            return result(responses[args])

        def fake_run(*args, **kwargs):
            nonlocal active
            events.append(args)
            if args == ("container", "list", "--all", "--format", "json"):
                return result(returncode=0 if service_running else 1)
            self.assertEqual(args, ("container", "system", "start"))
            active = ready
            return result()

        def fake_subprocess(args, **kwargs):
            nonlocal active
            events.append(tuple(args))
            if args == ["/usr/bin/sudo", "-v"]:
                return result()
            self.assertEqual(args[1], str(reboot.ROOT / "tests/integration.py"))
            self.assertIn("--network-checks", args)
            active = final
            if workflow_timeout:
                raise subprocess.TimeoutExpired(args, 600)
            network = {"success": network_passed, "checks": {key: network_passed for key in reboot.reboot_network.REQUIRED_CHECKS}}
            reboot.CLI_REPORT.write_text(json.dumps({"success": True, "checks": {"cli_passed": True},
                                                    "network": network, "final_state": "deleted"}))
            return result()

        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            for name, filename in [("BASELINE", "before.json"), ("REPORT", "report.json"), ("CLI_REPORT", "cli.json")]:
                stack.enter_context(patch.object(reboot, name, Path(temporary) / filename))
            reboot.BASELINE.write_text(json.dumps({"boot": {"uuid": "old"}, "helper_sha256": "same", "plist_sha256": "same"}))
            stack.enter_context(patch.object(reboot, "current_boot", return_value=boot))
            stack.enter_context(patch.object(reboot, "digest", return_value="same"))
            stack.enter_context(patch.object(reboot, "sudo", side_effect=fake_sudo))
            stack.enter_context(patch.object(reboot, "run", side_effect=fake_run))
            stack.enter_context(patch.object(reboot.subprocess, "run", side_effect=fake_subprocess))
            stack.enter_context(patch.object(reboot.time, "time", return_value=1012))
            stack.enter_context(redirect_stdout(io.StringIO()))
            error = None
            try:
                reboot.verify()
            except (RuntimeError, subprocess.TimeoutExpired) as failure:
                error = failure
            report = json.loads(reboot.REPORT.read_text())
        return report, events, error

    def test_service_start_changes_are_recorded_separately(self):
        report, events, error = self.verify_workflow()
        self.assertIsNone(error)
        self.assertTrue(report["success"])
        self.assertTrue(report["container_service_needed_start"])
        for kind in ("filter", "nat"):
            self.assertIn('+', report["main_rule_changes"]["container_service"][kind])
            self.assertEqual(report["main_rule_changes"]["box_workflow"][kind], "")
        self.assertLess(events.index(("/sbin/pfctl", "-s", "References")),
                        events.index(("container", "list", "--all", "--format", "json")))

    def test_running_service_does_not_restart(self):
        report, events, error = self.verify_workflow(service_running=True)
        self.assertIsNone(error)
        self.assertTrue(report["success"])
        self.assertFalse(report["container_service_needed_start"])
        self.assertNotIn(("container", "system", "start"), events)
        self.assertEqual(report["main_rule_changes"]["container_service"], {"filter": "", "nat": ""})

    def test_rule_changes_during_box_operations_still_fail(self):
        for kind, check in [("filter", "main_pf_rules_preserved"), ("nat", "main_nat_rules_preserved")]:
            with self.subTest(kind=kind):
                report, _, error = self.verify_workflow(changed_kind=kind)
                self.assertIsInstance(error, RuntimeError)
                self.assertIn(check, str(error))
                self.assertFalse(report["success"])
                self.assertFalse(report["checks"][check])
                self.assertIn("+unexpected rule", report["main_rule_changes"]["box_workflow"][kind])
                self.assertIn("unexpected rule", report["main_rules"]["after_box_workflow"][kind])

    def test_workflow_timeout_retains_rule_changes(self):
        report, _, error = self.verify_workflow(changed_kind="filter", workflow_timeout=True)
        self.assertIsInstance(error, subprocess.TimeoutExpired)
        self.assertFalse(report["success"])
        self.assertFalse(report["checks"]["main_pf_rules_preserved"])
        self.assertIn("+unexpected rule", report["main_rule_changes"]["box_workflow"]["filter"])

    def test_failed_network_checks_fail_the_reboot_test(self):
        report, _, error = self.verify_workflow(network_passed=False)
        self.assertIsInstance(error, RuntimeError)
        self.assertFalse(report["success"])
        self.assertFalse(report["checks"]["network_isolation_after_reboot"])
        self.assertIn("network_isolation_after_reboot", str(error))


if __name__ == "__main__":
    unittest.main()
