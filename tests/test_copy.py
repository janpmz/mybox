"""Check copy validation, independent configuration, and failure cleanup."""
import json
import unittest

import test_cli


class Copy(unittest.TestCase):
    setUp = test_cli.CLI.setUp
    run_box = test_cli.CLI.run_box
    calls = test_cli.CLI.calls

    def test_copy_preserves_source_and_creates_a_stopped_box_on_its_own_network(self):
        for state in ("normal", "stopped"):
            with self.subTest(state=state):
                self.environment["MYBOX_TEST_SCENARIO"] = state
                result = self.run_box("copy", "demo", "photo-box")
                calls = self.calls()
                commit = next(c for c in calls if c[0] == "commit")
                self.assertEqual(commit[1], "mybox-demo")
                create = next(c for c in calls if c[0] == "create")
                self.assertIn(commit[2], create)
                self.assertEqual(create[create.index("--name") + 1], "mybox-photo-box")
                self.assertEqual(create[create.index("--network") + 1], "mybox-net-photo-box,alias=photo-box.box.test")
                self.assertIn("com.mybox.network=mybox-net-photo-box", create)
                self.assertEqual(create[create.index("--cpus") + 1], "2")
                self.assertEqual(create[create.index("--memory") + 1], "2048m")
                self.assertFalse(any(c[0] in ("start", "stop", "exec", "host", "delete", "image") for c in calls))
                self.assertNotIn("--publish", create)
                self.assertNotIn("--volume", create)
                self.assertIn("new box is stopped", result.stdout)
                saved = json.loads(self.log.with_suffix(".state").read_text())
                self.assertEqual(saved["mybox-demo"]["status"]["state"], "stopped" if state == "stopped" else "running")
                self.assertEqual(saved["mybox-photo-box"]["status"]["state"], "stopped")
                self.log.unlink()
                self.log.with_suffix(".state").unlink()

    def test_invalid_arguments_do_not_call_the_backend(self):
        for arguments in [(), ("demo",), ("demo", "demo"), ("demo", "Uppercase"),
                          ("../demo", "photo-box"), ("demo", "photo-"), ("demo", "photo", "extra")]:
            self.run_box("copy", *arguments, success=False)
        self.assertEqual(self.calls(), [])

    def test_existing_target_is_rejected_before_snapshot(self):
        result = self.run_box("copy", "other", "demo", success=False)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(self.calls(), [["list", "--all", "--format", "json"]])

    def test_unsafe_or_unmanaged_source_is_rejected_before_snapshot(self):
        for scenario in ("unmanaged", "mount", "ssh", "public-port"):
            with self.subTest(scenario=scenario):
                self.environment["MYBOX_TEST_SCENARIO"] = scenario
                self.run_box("copy", "demo", "photo-box", success=False)
                self.assertTrue(all(c[0] in ("list", "inspect") for c in self.calls()))

    def test_copy_preserves_custom_limits_but_not_ports(self):
        self.environment["MYBOX_TEST_SCENARIO"] = "stopped"
        self.run_box("ports", "add", "demo", "--inside-port", "8080", "--outside-port", "8766")
        self.run_box("copy", "demo", "first-copy")
        path = self.log.with_suffix(".state")
        state = json.loads(path.read_text())
        source = state["mybox-demo"]["configuration"]
        source["resources"] = dict(cpus=5, memoryInBytes=768 * 1024 ** 2)
        source["publishedPorts"] = [dict(hostAddress="127.0.0.1", proto="tcp", count=1, hostPort=8767, containerPort=8081)]
        path.write_text(json.dumps(state))
        self.log.write_text("")
        self.run_box("copy", "demo", "photo-box")
        create = next(c for c in self.calls() if c[0] == "create")
        self.assertEqual(create[create.index("--cpus") + 1], "5")
        self.assertEqual(create[create.index("--memory") + 1], "768m")
        self.assertNotIn("--publish", create)
        self.assertFalse((self.directory / "ports/mybox-photo-box").exists())
        self.assertEqual(json.loads(path.read_text())["mybox-demo"], state["mybox-demo"])

    def test_failures_remove_only_the_unused_copy_image_and_its_new_network(self):
        for failure in (["commit"], ["network", "create"], ["create"]):
            with self.subTest(failure=failure):
                self.environment["MYBOX_TEST_BACKEND_FAIL"] = json.dumps(failure)
                self.run_box("copy", "demo", "photo-box", success=False)
                calls = self.calls()
                image = next(c[2] for c in calls if c[0] == "commit")
                self.assertIn(["image", "delete", "--force", image], calls)
                self.assertFalse(any(c[0] in ("start", "stop", "delete", "exec") for c in calls))
                self.assertEqual([c for c in calls if c[:2] == ["network", "delete"]],
                                 [["network", "delete", "mybox-net-photo-box"]] if failure == ["create"] else [])
                self.log.unlink()

    def test_copied_box_still_requires_the_trusted_helper_and_firewall_before_start(self):
        self.run_box("copy", "demo", "photo-box")
        self.environment["MYBOX_TEST_FIREWALL_FAIL"] = "verify"
        self.run_box("start", "photo-box", success=False)
        calls = self.calls()
        helper = next(c for c in calls if c[0] == "create" and "--read-only" in c)
        self.assertIn("docker.io/library/debian:trixie-slim", helper)
        self.assertNotIn(["start", "mybox-photo-box"], calls)


if __name__ == "__main__":
    unittest.main()
