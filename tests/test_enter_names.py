"""Check partial box names without contacting the container service."""
import json
import unittest

import test_cli


class EnterNames(unittest.TestCase):
    setUp = test_cli.CLI.setUp
    run_box = test_cli.CLI.run_box
    calls = test_cli.CLI.calls

    def box(self, name, state="running"):
        network = "mybox-net-" + name
        return {
            "id": "mybox-" + name,
            "configuration": {
                "creationDate": "2026-11-04T10:00:00Z",
                "labels": {"com.mybox.managed": "true", "com.mybox.network": network},
                "mounts": [], "publishedSockets": [], "publishedPorts": [], "ssh": False,
                "networks": [{"network": network}],
            },
            "status": {"state": state, "startedDate": "2026-11-04T10:01:00Z",
                       "networks": [{"network": network, "ipv4Address": "192.168.65.3/24"}]},
        }

    def save_boxes(self, *boxes):
        self.log.with_suffix(".state").write_text(json.dumps({box["id"]: box for box in boxes}))

    def test_prefix_and_middle_of_name_open_the_matching_box(self):
        self.save_boxes(self.box("document-player-box"), self.box("web"))
        for query in ("doc", "player", "box"):
            with self.subTest(query=query):
                result = self.run_box("enter", query)
                self.assertEqual(json.loads(result.stdout)[-2:], ["mybox-document-player-box", "/bin/bash"])
                self.assertIn("Using box document-player-box.", result.stderr)
        self.assertFalse(any(call[0] == "start" for call in self.calls()))

    def test_exact_name_takes_priority(self):
        self.save_boxes(self.box("doc"), self.box("document-player-box"), self.box("my-doc"))
        result = self.run_box("enter", "doc")
        self.assertEqual(json.loads(result.stdout)[-2:], ["mybox-doc", "/bin/bash"])
        self.assertNotIn("Using box", result.stderr)

    def test_ambiguous_name_lists_all_matches_without_entering_or_starting(self):
        self.save_boxes(self.box("document-player-box", "stopped"), self.box("docs"), self.box("web"))
        result = self.run_box("enter", "doc", success=False)
        self.assertIn('Several MyBox names contain "doc":\n  docs\n  document-player-box\n', result.stderr)
        self.assertIn("Type more of the name", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.calls(), [["list", "--all", "--format", "json"]])

    def test_missing_name_reports_how_to_list_boxes(self):
        for boxes in ([], [self.box("web")]):
            with self.subTest(boxes=boxes):
                self.save_boxes(*boxes)
                result = self.run_box("enter", "doc", success=False)
                self.assertIn('No MyBox name contains "doc".', result.stderr)
                self.assertIn("mybox list", result.stderr)
        self.assertTrue(all(call == ["list", "--all", "--format", "json"] for call in self.calls()))

    def test_search_ignores_unmanaged_containers_and_helpers(self):
        unmanaged = self.box("doc")
        unmanaged["configuration"]["labels"] = {}
        helper = self.box("helper-doc")
        helper["configuration"]["labels"] = {"com.mybox.helper": "true"}
        foreign = self.box("doc-foreign")
        foreign["id"] = "doc-foreign"
        self.save_boxes(unmanaged, helper, foreign, self.box("document-player-box"))
        result = self.run_box("enter", "doc")
        self.assertEqual(json.loads(result.stdout)[-2:], ["mybox-document-player-box", "/bin/bash"])

    def test_partial_name_starts_a_stopped_box_with_firewall_checks(self):
        self.environment["MYBOX_TEST_SCENARIO"] = "stopped"
        result = self.run_box("enter", "dem")
        self.assertEqual(json.loads(result.stdout)[-2:], ["mybox-demo", "/bin/bash"])
        calls = self.calls()
        apply = next(i for i, call in enumerate(calls) if call[:2] == ["host", "/usr/bin/sudo"] and call[3] == "apply")
        self.assertLess(apply, calls.index(["start", "mybox-demo"]))

    def test_partial_name_cannot_bypass_configuration_or_firewall_checks(self):
        for scenario, failure in (("mount", ""), ("normal", "verify"), ("stopped", "apply")):
            with self.subTest(scenario=scenario):
                self.environment.update(MYBOX_TEST_SCENARIO=scenario, MYBOX_TEST_FIREWALL_FAIL=failure)
                self.run_box("enter", "dem", success=False)
        self.assertNotIn(["start", "mybox-demo"], self.calls())
        self.assertFalse(any(call[0] == "exec" for call in self.calls()))

    def test_command_arguments_and_exit_status_survive_partial_match(self):
        self.environment["MYBOX_TEST_EXIT"] = "37"
        argument = "literal ' ; $(touch /tmp/do-not-run)"
        result = self.run_box("enter", "dem", "--", "/bin/echo", argument, success=False)
        self.assertEqual(result.returncode, 37)
        self.assertEqual(json.loads(result.stdout)[-3:], ["mybox-demo", "/bin/echo", argument])
        self.assertIn("Using box demo.", result.stderr)

    def test_invalid_queries_do_not_reach_backend(self):
        for query in ("", "--all", "../doc", "doc*", "doc\n"):
            with self.subTest(query=query):
                self.run_box("enter", query, success=False)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
