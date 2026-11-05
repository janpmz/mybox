"""Check stopped resource changes and disk replacement failures."""
import json
import re
import unittest
from datetime import datetime, timezone

import test_cli


class Resources(unittest.TestCase):
    setUp = test_cli.CLI.setUp
    run_box = test_cli.CLI.run_box
    calls = test_cli.CLI.calls

    def prepare(self, disk=True, nested=False):
        self.environment["MYBOX_TEST_SCENARIO"] = "stopped"
        self.run_box("copy", "demo", "unused-copy")
        state = json.loads(self.log.with_suffix(".state").read_text())
        self.original = state["mybox-demo"]["configuration"]
        self.original["resources"]["cpuOverhead"] = 1
        self.log.with_suffix(".state").write_text(json.dumps(state))
        self.original["creationDate"] = (datetime(2026, 11, 4, 10, tzinfo=timezone.utc)
                                         - datetime(2001, 1, 1, tzinfo=timezone.utc)).total_seconds()
        self.box = self.container_root / "mybox-demo"
        self.box.mkdir(parents=True)
        self.config = self.box / ("runtime-configuration.json" if nested else "config.json")
        content = dict(containerConfiguration=self.original, options=dict(autoRemove=False)) if nested else self.original
        self.config.write_text(json.dumps(content))
        self.disk = self.box / "rootfs.ext4"
        if disk:
            with self.disk.open("wb") as stream:
                stream.write(b"original disk data")
                stream.truncate(2 * 1024 ** 3)
            (self.box / "rootfs.json").write_text(json.dumps(dict(
                source=str(self.disk), destination="/", type=dict(block=dict(format="ext4")), options=[])))
        self.log.write_text("")

    def test_invalid_options_never_reach_backend(self):
        cases = [("../demo", "--cpus", "2"), ("demo", "--cpus"),
                 ("demo", "--cpus", "0"), ("demo", "--cpus", "1.5"),
                 ("demo", "--cpus", "99999999999999999999999999999"),
                 ("demo", "--cpus", "2", "--cpus", "3"),
                 ("demo", "--memory", "4"),
                 ("demo", "--disk-size-gb", "0.12"), ("demo", "--disk-size-gb", "0"),
                 ("demo", "--disk-size", "4"), ("demo", "--disk-size-gb", "4g"),
                 ("demo", "--memory-gb", "2g"), ("demo", "--memory-gb", "0.19"),
                 ("demo", "--memory-gb", "1e2"), ("demo", "--memory-gb", "-1"),
                 ("demo", "--memory-gb", "99999999999999999"),
                 ("demo", "--disk-size-gb", "1", "extra")]
        for args in cases:
            with self.subTest(args=args):
                self.run_box("resize", *args, success=False)
        self.assertEqual(self.calls(), [])

    def test_running_and_unsafe_boxes_are_rejected(self):
        for scenario in ("normal", "unmanaged", "mount", "ssh", "public-port"):
            self.environment["MYBOX_TEST_SCENARIO"] = scenario
            self.run_box("resize", "demo", "--cpus", "4", success=False)
        self.assertTrue(all(c[0] == "inspect" for c in self.calls()))

    def test_numbered_resize_retries_bad_inputs_and_applies_choices(self):
        self.prepare()
        self.log.with_suffix(".service").write_text("unregistered")
        result = self.run_box("resize", answers="9\n1\n-2\n1000000\n3\n\nwrong\n1\n")
        self.assertIn("1. demo\n2. unused-copy", result.stderr)
        self.assertIn("Enter a number from 1 to 2", result.stderr)
        self.assertEqual(len(re.findall(r"CPUs \[2\] \(\d+ CPUs available\):", result.stderr)), 3)
        self.assertRegex(result.stderr, r"Memory in GB \[2\] \(([\d.,]+ GB available of [\d.,]+ GB|[\d.,]+ GB total, free unknown)\):")
        self.assertEqual(len(re.findall(r"Disk in GB \[2[.,]147\] \([\d.,]+ GB available of [\d.,]+ GB\):", result.stderr)), 2)
        self.assertIn("Box: demo\n", result.stderr)
        self.assertNotIn("maximum", result.stderr.lower())
        saved = json.loads(self.config.read_text())["resources"]
        self.assertEqual(saved["cpus"], 3)
        self.assertEqual(saved["memoryInBytes"], self.original["resources"]["memoryInBytes"])
        self.assertEqual(self.disk.stat().st_size, 954 * 1024 ** 2)

    def test_single_stopped_box_skips_menu_and_blank_values_keep_settings(self):
        self.prepare()
        statefile = self.log.with_suffix(".state")
        state = json.loads(statefile.read_text())
        state["mybox-unused-copy"]["status"]["state"] = "running"
        statefile.write_text(json.dumps(state))
        original = self.config.read_bytes()
        result = self.run_box("resize", answers="\n\n\n")
        self.assertNotIn("Box number:", result.stderr)
        self.assertIn("Kept the current settings for demo", result.stdout)
        self.assertEqual(self.config.read_bytes(), original)

    def test_end_of_input_discards_partial_answers(self):
        self.prepare()
        original = self.config.read_bytes()
        result = self.run_box("resize", "demo", answers="4\n0.5\n", success=False)
        self.assertIn("Resize canceled", result.stderr)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertTrue(all(c[0] == "inspect" for c in self.calls()))

    def test_no_disk_yet_allows_interactive_memory_change(self):
        self.prepare(disk=False, nested=True)
        result = self.run_box("resize", "demo", answers="\n0.5\n")
        self.assertIn("Disk size becomes available after the first start", result.stderr)
        saved = json.loads(self.config.read_text())["containerConfiguration"]["resources"]
        self.assertEqual(saved["memoryInBytes"], 512 * 1024 ** 2)

    def test_cpu_and_memory_change_preserves_disk_and_other_configuration(self):
        self.prepare()
        before = self.disk.stat()
        result = self.run_box("resize", "demo", "--cpus", "4", "--memory-gb", "4")
        expected = json.loads(json.dumps(self.original))
        expected["resources"].update(cpus=4, memoryInBytes=4 * 1024 ** 3)
        self.assertEqual(json.loads(self.config.read_text()), expected)
        self.assertEqual(self.disk.stat().st_ino, before.st_ino)
        self.assertEqual(result.stdout.splitlines(), ["Updated demo.", "Start it with: mybox start demo"])
        self.assertTrue(all(c[0] == "inspect" for c in self.calls()))
        listing = self.run_box("list").stdout
        self.assertIn("4 GB", listing)
        self.run_box("copy", "demo", "resized-copy")
        create = next(c for c in self.calls() if c[0] == "create")
        self.assertEqual(create[create.index("--cpus") + 1], "4")
        self.assertEqual(create[create.index("--memory") + 1], "4096m")
        self.run_box("resize", "demo", "--cpus", "1")
        expected["resources"]["cpus"] = 1
        self.assertEqual(json.loads(self.config.read_text()), expected)

    def test_cpu_and_memory_work_before_first_start(self):
        self.prepare(disk=False, nested=True)
        self.run_box("resize", "demo", "--memory-gb", "0.5")
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved["containerConfiguration"]["resources"]["memoryInBytes"], 512 * 1024 ** 2)
        self.assertEqual(saved["options"], dict(autoRemove=False))
        self.run_box("resize", "demo", "--disk-size-gb", "1", success=False)
        self.assertTrue(all(c[0] == "inspect" for c in self.calls()))

    def test_disk_replacement_uses_isolated_helper_and_keeps_configuration(self):
        self.prepare()
        self.run_box("resize", "demo", "--disk-size-gb", "1")
        self.assertEqual(self.disk.stat().st_size, 954 * 1024 ** 2)
        self.assertEqual(json.loads(self.config.read_text()), self.original)
        calls = self.calls()
        helper = next(c for c in calls if c[0] == "create")
        self.assertEqual(helper[helper.index("--network") + 1], "none")
        self.assertIn("--read-only", helper)
        self.assertIn("docker.io/library/debian:trixie-slim", helper)
        self.assertIn("readonly", helper[helper.index("--mount") + 1])
        self.assertTrue(all(c[1] != "mybox-demo" for c in calls if c[0] in ("start", "stop", "delete", "exec")))
        self.assertFalse(any((self.directory / "volumes").iterdir()))
        self.assertFalse(list(self.box.glob(".mybox-resize-*")))

    def test_same_disk_size_does_not_copy(self):
        self.prepare()
        before = self.disk.stat().st_ino
        prompts = self.run_box("resize", "demo", answers="\n\n\n").stderr
        disk_value = re.search(r"Disk in GB \[([\d.,]+)\]", prompts)[1]
        self.run_box("resize", "demo", "--disk-size-gb", disk_value)
        self.assertEqual(before, self.disk.stat().st_ino)
        self.assertTrue(all(c[0] == "inspect" for c in self.calls()))

    def test_limits_above_host_capacity_do_not_change_disk_or_settings(self):
        self.prepare()
        before = self.config.read_bytes(), self.disk.stat().st_ino
        for option, value in (("--cpus", "1000000"), ("--memory-gb", "999999999")):
            result = self.run_box("resize", "demo", option, value, "--disk-size-gb", "1", success=False)
            self.assertIn("at most " + option, result.stderr)
            self.assertEqual((self.config.read_bytes(), self.disk.stat().st_ino), before)
        self.assertTrue(all(c[0] == "inspect" for c in self.calls()))

    def test_capacity_report_and_high_allocation_warnings(self):
        self.prepare(disk=False)
        info = self.run_box("resize", "demo", "--memory-gb", "0.5002").stderr
        self.assertEqual(json.loads(self.config.read_text())["resources"]["memoryInBytes"], 513 * 1024 ** 2)
        self.assertRegex(info, r"RAM ([\d.,]+ GB available of [\d.,]+ GB|[\d.,]+ GB total, free unknown)")
        host_cpus = int(re.search(r"Mac: (\d+) CPUs", info)[1])
        self.assertNotIn("maximum", info.lower())
        def highest_allowed(option):
            error = self.run_box("resize", "demo", option, "999999", success=False).stderr
            return re.search(r"at most " + option + r" ([\d.]+) on", error)[1]
        cpus = highest_allowed("--cpus")
        memory = highest_allowed("--memory-gb")
        result = self.run_box("resize", "demo", "--cpus", cpus, "--memory-gb", memory)
        if int(cpus) + 1 > host_cpus:
            self.assertIn(f"They share {host_cpus} logical CPUs", result.stderr)
        elif int(cpus) + 1 >= host_cpus * 0.8:
            self.assertIn("80% of the Mac's CPU capacity", result.stderr)
        host_ram = re.search(r"RAM [\d.]+ GB available of ([\d.]+) GB", info.replace(",", "."))
        if host_ram and float(memory) + 0.125 >= float(host_ram[1]) * 0.8:
            self.assertRegex(result.stderr, r"Warning: The VM (needs|would use)")

    def test_copy_and_helper_failures_preserve_original_disk_and_settings(self):
        self.prepare()
        before = self.disk.stat().st_ino
        for failure in (["volume", "create"], ["create"], ["start"], ["exec"], ["stop"], ["delete"]):
            with self.subTest(failure=failure):
                self.environment["MYBOX_TEST_BACKEND_FAIL"] = json.dumps(failure)
                self.run_box("resize", "demo", "--cpus", "4", "--disk-size-gb", "1", success=False)
                self.assertEqual(json.loads(self.config.read_text()), self.original)
                self.assertEqual(before, self.disk.stat().st_ino)
                with self.disk.open("rb") as stream:
                    self.assertEqual(stream.read(18), b"original disk data")
                self.assertFalse(list(self.box.glob(".mybox-resize-*")))

    def test_configuration_identity_and_links_are_checked_before_writes(self):
        self.prepare()
        original = self.config.read_text()
        for key, value in (("id", "mybox-other"), ("creationDate", "different"), ("ssh", True)):
            changed = json.loads(original)
            changed[key] = value
            self.config.write_text(json.dumps(changed))
            self.run_box("resize", "demo", "--cpus", "4", success=False)
            self.assertEqual(json.loads(self.config.read_text()), changed)
        self.config.write_text(original)
        self.disk.unlink()
        self.disk.symlink_to(self.config)
        self.run_box("resize", "demo", "--disk-size-gb", "1", success=False)
        self.assertEqual(self.config.read_text(), original)
        self.assertTrue(all(c[0] == "inspect" for c in self.calls()))


if __name__ == "__main__":
    unittest.main()
