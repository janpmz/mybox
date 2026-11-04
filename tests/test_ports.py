"""Check port commands against fake container and firewall tools."""
import json
from pathlib import Path
import unittest

import test_cli


class Ports(unittest.TestCase):
    setUp = test_cli.CLI.setUp
    run_box = test_cli.CLI.run_box
    calls = test_cli.CLI.calls

    def saved(self):
        return json.loads((self.directory / "ports/mybox-demo/ports.json").read_text())

    def test_add_and_remove_do_not_restart_or_execute_in_the_box(self):
        self.run_box("ports", "add", "demo", "--inside-port", "8080", "--outside-port", "8766")
        self.assertEqual(self.saved()["mappings"], [{"inside": 8080, "outside": 8766}])
        actions = [c[3] for c in self.calls() if c[:2] == ["host", "/usr/bin/sudo"]]
        self.assertEqual(actions, ["verify", "update-ports"])
        self.run_box("ports", "remove", "demo", "--outside-port", "8766")
        self.assertEqual(self.saved()["mappings"], [])
        self.assertFalse(any(c[0] in ("start", "stop", "create", "delete", "exec") for c in self.calls()))

    def test_stopped_box_saves_ports_without_starting_or_changing_firewall(self):
        self.environment["MYBOX_TEST_SCENARIO"] = "stopped"
        result = self.run_box("ports", "add", "demo", "--inside-port", "8080", "--outside-port", "8766")
        self.assertIn("remains stopped", result.stdout)
        self.assertNotIn("target", self.saved())
        self.assertFalse(any(c[0] == "start" or c[:2] == ["host", "/usr/bin/sudo"] for c in self.calls()))
        self.assertIn("saved", self.run_box("ports", "list", "demo").stdout)
        self.run_box("start", "demo")
        self.assertEqual(self.saved()["target"]["address"], "192.168.65.3")

    def test_duplicate_and_invalid_ports_do_not_change_saved_settings(self):
        self.run_box("ports", "add", "demo", "--inside-port", "8080", "--outside-port", "8766")
        before = self.saved()
        for args in [("ports", "add", "demo", "--inside-port", "9000", "--outside-port", "8766"),
                     ("ports", "remove", "demo", "--outside-port", "1234")]:
            self.run_box(*args, success=False)
        self.assertEqual(self.saved(), before)
        self.log.write_text("")
        for args in [("ports", "add", "demo"), ("ports", "remove", "demo"),
                     ("ports", "list", "demo", "--outside-port", "8766"),
                     ("ports", "add", "demo", "--inside-port", "0", "--outside-port", "8766"),
                     ("ports", "add", "demo", "--inside-port", "80", "--outside-port", "65536"),
                     ("ports", "add", "demo", "--inside-port", "80", "--outside-port", "9000", "--outside-port", "9001")]:
            self.run_box(*args, success=False)
        self.assertEqual(self.calls(), [])

    def test_failed_forwarding_restores_saved_settings(self):
        self.environment["MYBOX_TEST_FORWARDER_FAIL"] = "1"
        self.run_box("ports", "add", "demo", "--inside-port", "8080", "--outside-port", "8766", success=False)
        self.assertEqual(self.saved()["mappings"], [])
        self.assertFalse((self.directory / "ports/mybox-demo/pending.json").exists())

    def test_interrupted_change_recovers_before_enter(self):
        self.run_box("ports", "add", "demo", "--inside-port", "8080", "--outside-port", "8766")
        original = self.saved()
        directory = self.directory / "ports/mybox-demo"
        (directory / "pending.json").write_text(json.dumps(original))
        changed = dict(original, mappings=[{"inside": 9000, "outside": 9001}])
        (directory / "ports.json").write_text(json.dumps(changed))
        self.run_box("enter", "demo", "--", "/bin/true")
        self.assertEqual(self.saved()["mappings"], original["mappings"])
        self.assertFalse((directory / "pending.json").exists())

    def test_existing_apple_mapping_is_listed_and_cannot_be_removed(self):
        self.run_box("start", "demo")
        path = self.log.with_suffix(".state")
        # The initial fake running box does not write a state file on inspect.
        if not path.exists():
            self.environment["MYBOX_TEST_SCENARIO"] = "stopped"
            self.run_box("start", "demo")
        state = json.loads(path.read_text())
        state["mybox-demo"]["configuration"]["publishedPorts"] = [dict(hostAddress="127.0.0.1", proto="tcp", count=1, hostPort=8766, containerPort=8080)]
        path.write_text(json.dumps(state))
        self.assertIn("Apple", self.run_box("ports", "list", "demo").stdout)
        result = self.run_box("ports", "remove", "demo", "--outside-port", "8766", success=False)
        self.assertIn("Apple manages", result.stderr)
        self.run_box("ports", "add", "demo", "--inside-port", "8081", "--outside-port", "8767")
        policies = [json.loads(c[4]) for c in self.calls() if c[:2] == ["host", "/usr/bin/sudo"] and c[3] == "update-ports"]
        self.assertEqual(policies[-1]["ports"], [8080, 8081])

    def test_saved_ports_do_not_transfer_to_a_reused_container_name(self):
        self.run_box("ports", "add", "demo", "--inside-port", "8080", "--outside-port", "8766")
        saved = self.saved()
        saved["created"] = "old-container"
        (self.directory / "ports/mybox-demo/ports.json").write_text(json.dumps(saved))
        self.assertIn("No port mappings", self.run_box("ports", "list", "demo").stdout)


if __name__ == "__main__":
    unittest.main()
