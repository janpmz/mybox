"""Deletion checks use a fake backend and disposable files, never real boxes."""
import json
import unittest

import test_cli


class Delete(unittest.TestCase):
    setUp = test_cli.CLI.setUp
    run_box = test_cli.CLI.run_box
    calls = test_cli.CLI.calls

    def test_invalid_arguments_do_not_contact_backend(self):
        for args in [(), ("demo", "extra"), ("demo", "--force"), ("demo", "--yes", "--yes"),
                     ("--all",), ("../demo",), ("Uppercase",), ("demo\n",)]:
            self.run_box("delete", *args, success=False)
        self.assertEqual(self.calls(), [])

    def test_confirmation_defaults_to_cancel_and_never_changes_box_or_ports(self):
        for answer in ["\n", "n\n", "no\n", "anything\n", ""]:
            with self.subTest(answer=answer):
                result = self.run_box("delete", "demo", answers=answer)
                self.assertIn("all its files? [y/N]", result.stderr)
                self.assertIn("canceled", result.stdout)
                self.assertTrue(all(call[0] in ("list", "network") for call in self.calls()))
                self.assertFalse(self.log.with_suffix(".state").exists())
                self.assertFalse((self.directory / "ports").exists())

    def test_yes_confirmation_deletes_only_the_named_box(self):
        self.run_box("copy", "demo", "keep-me")
        before = json.loads(self.log.with_suffix(".state").read_text())["mybox-keep-me"]
        self.run_box("delete", "demo", answers="yes\n")
        after = json.loads(self.log.with_suffix(".state").read_text())
        self.assertEqual(after, {"mybox-keep-me": before})

    def test_running_and_stopped_boxes_remove_ports_firewall_then_disk_and_network(self):
        for state in ("normal", "stopped"):
            with self.subTest(state=state):
                self.environment["MYBOX_TEST_SCENARIO"] = state
                self.run_box("ports", "add", "demo", "--inside-port", "8080", "--outside-port", "8766")
                self.log.write_text("")
                result = self.run_box("delete", "demo", "--yes")
                calls = self.calls()
                deletion = calls.index(["delete", "mybox-demo"])
                firewall = next(i for i, c in enumerate(calls) if c[:2] == ["host", "/usr/bin/sudo"] and c[3] == "remove")
                self.assertLess(firewall, deletion)
                self.assertLess(deletion, calls.index(["network", "delete", "mybox-net-demo"]))
                if state == "normal":
                    self.assertLess(calls.index(["stop", "mybox-demo"]), firewall)
                else:
                    self.assertNotIn(["stop", "mybox-demo"], calls)
                self.assertFalse(any(c[0] in ("start", "exec", "image") for c in calls))
                self.assertEqual(json.loads(self.log.with_suffix(".state").read_text()), {})
                self.assertFalse((self.directory / "ports/mybox-demo").exists())
                self.assertIn("Deleted demo.", result.stdout)
                self.log.with_suffix(".state").unlink()
                self.log.write_text("")

    def test_full_name_is_required_even_when_partial_name_is_unique(self):
        result = self.run_box("delete", "dem", "--yes", success=False)
        self.assertIn("full name", result.stderr)
        self.assertEqual(self.calls(), [["list", "--all", "--format", "json"]])

    def test_unmanaged_unsafe_and_shared_networks_are_not_deleted(self):
        for scenario in ("unmanaged", "mount", "ssh", "public-port", "foreign-member"):
            with self.subTest(scenario=scenario):
                self.environment["MYBOX_TEST_SCENARIO"] = scenario
                self.run_box("delete", "demo", "--yes", success=False)
                self.assertTrue(all(c[0] in ("list", "network") for c in self.calls()))
                self.assertFalse(any(c[:2] == ["network", "delete"] for c in self.calls()))

    def test_legacy_box_deletion_preserves_its_shared_network(self):
        self.environment["MYBOX_TEST_SCENARIO"] = "legacy"
        self.run_box("delete", "demo", "--yes")
        self.assertIn(["delete", "mybox-demo"], self.calls())
        self.assertFalse(any(c[0] in ("network", "host") for c in self.calls()))

    def test_stop_or_firewall_failure_preserves_disk_and_network(self):
        for failure in ("stop", "firewall"):
            with self.subTest(failure=failure):
                if failure == "stop":
                    self.environment["MYBOX_TEST_BACKEND_FAIL"] = '["stop"]'
                else:
                    self.environment.pop("MYBOX_TEST_BACKEND_FAIL", None)
                    self.environment["MYBOX_TEST_FIREWALL_FAIL"] = "remove"
                self.run_box("delete", "demo", "--yes", success=False)
                self.assertNotIn(["delete", "mybox-demo"], self.calls())
                self.assertNotIn(["network", "delete", "mybox-net-demo"], self.calls())

    def test_backend_delete_failure_keeps_port_state_for_retry(self):
        self.run_box("ports", "add", "demo", "--inside-port", "8080", "--outside-port", "8766")
        self.environment["MYBOX_TEST_BACKEND_FAIL"] = '["delete"]'
        self.run_box("delete", "demo", "--yes", success=False)
        self.assertIn("mybox-demo", json.loads(self.log.with_suffix(".state").read_text()))
        self.assertTrue((self.directory / "ports/mybox-demo/ports.json").exists())
        self.assertNotIn(["network", "delete", "mybox-net-demo"], self.calls())
        self.environment.pop("MYBOX_TEST_BACKEND_FAIL")
        self.run_box("delete", "demo", "--yes")
        self.assertFalse((self.directory / "ports/mybox-demo").exists())

    def test_network_cleanup_failure_can_be_retried_after_box_is_gone(self):
        self.environment["MYBOX_TEST_BACKEND_FAIL"] = '["network", "delete"]'
        result = self.run_box("delete", "demo", "--yes", success=False)
        self.assertIn("was deleted, but cleanup failed", result.stderr)
        self.assertIn("mybox delete demo --yes", result.stderr)
        self.assertEqual(json.loads(self.log.with_suffix(".state").read_text()), {})
        self.assertTrue((self.directory / "ports/mybox-demo/delete.json").exists())
        self.environment.pop("MYBOX_TEST_BACKEND_FAIL")
        self.log.write_text("")
        self.run_box("delete", "demo", "--yes")
        self.assertNotIn(["delete", "mybox-demo"], self.calls())
        self.assertIn(["network", "delete", "mybox-net-demo"], self.calls())
        self.assertFalse((self.directory / "ports/mybox-demo").exists())

    def test_retry_refuses_a_replacement_box_with_the_same_name(self):
        self.environment["MYBOX_TEST_BACKEND_FAIL"] = '["delete"]'
        self.run_box("delete", "demo", "--yes", success=False)
        path = self.log.with_suffix(".state")
        state = json.loads(path.read_text())
        state["mybox-demo"]["configuration"]["creationDate"] = "new-box"
        path.write_text(json.dumps(state))
        self.environment.pop("MYBOX_TEST_BACKEND_FAIL")
        self.log.write_text("")
        result = self.run_box("delete", "demo", "--yes", success=False)
        self.assertIn("identity", result.stderr)
        self.assertEqual(self.calls(), [["list", "--all", "--format", "json"]])

    def test_port_directory_symlink_is_not_followed_or_removed(self):
        target = self.directory / "keep"
        target.mkdir()
        (target / "important").write_text("keep me")
        ports = self.directory / "ports"
        ports.mkdir()
        (ports / "mybox-demo").symlink_to(target)
        self.run_box("delete", "demo", "--yes", success=False)
        self.assertEqual((target / "important").read_text(), "keep me")
        self.assertEqual(self.calls(), [])

    def test_private_copy_image_is_removed_but_shared_base_image_is_kept(self):
        image = 'docker.io/library/mybox-copy-12345678-1234-1234-1234-123456789abc:snapshot'
        self.run_box("stop", "demo")
        path = self.log.with_suffix(".state")
        original = json.loads(path.read_text())
        for reference, shared in [(image, False), (image, True), ('docker.io/library/debian:trixie-slim', False)]:
            with self.subTest(reference=reference, shared=shared):
                state = json.loads(json.dumps(original))
                state['mybox-demo']['configuration']['image'] = dict(reference=reference, descriptor=dict(digest='sha256:copy'))
                if shared:
                    other = json.loads(json.dumps(state['mybox-demo']))
                    other['id'] = 'other-box'
                    other['configuration']['networks'] = []
                    other['configuration']['image']['reference'] = 'another-tag'
                    state['other-box'] = other
                path.write_text(json.dumps(state))
                self.log.write_text('')
                self.run_box('delete', 'demo', '--yes')
                images = [c for c in self.calls() if c[:2] == ['image', 'delete']]
                self.assertEqual(images, [['image', 'delete', '--force', image]] if reference == image and not shared else [])

    def test_port_root_symlink_is_rejected_before_deletion(self):
        target = self.directory / "keep"
        target.mkdir(mode=0o700)
        (self.directory / "ports").symlink_to(target)
        self.run_box("delete", "demo", "--yes", success=False)
        self.assertTrue(target.exists())
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
