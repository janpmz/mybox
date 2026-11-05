"""Offline black-box checks. The fake backend never contacts the container service."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BOX = str(ROOT / ".build" / "mybox-test")

FAKE_BACKEND = r'''#!/usr/bin/python3
import json, os, pathlib, sys
args = sys.argv[1:]
logpath = pathlib.Path(os.environ["MYBOX_TEST_LOG"])
if args[:1] == ["system"]:
    service = logpath.with_suffix(".service")
    with logpath.with_suffix(".service-log").open("a") as log:
        log.write(json.dumps(args) + "\n")
    if args == ["system", "status", "--format", "json"]:
        status = service.read_text() if service.exists() else "running"
        print(json.dumps({"status": status}))
        sys.exit(0 if status == "running" else 1)
    if args == ["system", "start", "--disable-kernel-install"]:
        if os.environ.get("MYBOX_TEST_SERVICE_FAIL"):
            print("test service startup failure", file=sys.stderr)
            sys.exit(1)
        service.write_text("running")
        sys.exit(0)
    sys.exit(2)
with logpath.open("a") as log:
    log.write(json.dumps(args) + "\n")
failure = json.loads(os.environ.get("MYBOX_TEST_BACKEND_FAIL", "[]"))
if failure and args[:len(failure)] == failure:
    sys.exit(1)
statepath = logpath.with_suffix(".state")
scenario = os.environ.get("MYBOX_TEST_SCENARIO", "normal")
configuration = {
    "id": "mybox-demo",
    "creationDate": "2026-11-04T10:00:00Z",
    "labels": {"io.github.janpmz.my-container-box.managed": "true", "io.github.janpmz.my-container-box.network": "mybox-net-demo"}, "mounts": [], "ssh": False,
    "resources": {"cpus": 2, "memoryInBytes": 2147483648},
    "publishedSockets": [], "publishedPorts": [], "networks": [{"network": "mybox-net-demo"}]
}
if scenario == "unmanaged": configuration["labels"] = {}
if scenario == "legacy": configuration["labels"].pop("io.github.janpmz.my-container-box.network")
if scenario == "mount": configuration["mounts"] = [{"source": "/Users/example"}]
if scenario == "ssh": configuration["ssh"] = True
if scenario == "public-port":
    configuration["publishedPorts"] = [{"hostAddress": "0.0.0.0", "proto": "tcp"}]
status = {"state": "stopped" if scenario == "stopped" else "running", "startedDate": "2026-11-04T10:01:00Z",
          "networks": [{"network": "mybox-net-demo", "ipv4Address": "192.168.65.3/24"}]}
state = json.loads(statepath.read_text()) if statepath.exists() else {"mybox-demo": {"id": "mybox-demo", "configuration": configuration, "status": status}}
if args[:2] == ["volume", "create"]:
    name = args[-1]
    directory = pathlib.Path(os.environ["MYBOX_TEST_CONTAINER_ROOT"]).parent / "volumes" / name
    directory.mkdir(parents=True)
    with (directory / "volume.img").open("wb") as stream:
        journal = int(args[args.index("--opt")+1].split(":")[1])
        stream.truncate(int(args[args.index("-s")+1]) + journal)
elif args[:2] == ["volume", "inspect"]:
    name = args[2]
    path = pathlib.Path(os.environ["MYBOX_TEST_CONTAINER_ROOT"]).parent / "volumes" / name / "volume.img"
    print(json.dumps([dict(id=name, configuration=dict(name=name, format="ext4", source=str(path)))]))
elif args[:2] == ["volume", "delete"]:
    import shutil
    path = pathlib.Path(os.environ["MYBOX_TEST_CONTAINER_ROOT"]).parent / "volumes" / args[2]
    if path.exists(): shutil.rmtree(path)
elif args[0] == "create":
    name = args[args.index("--name")+1]
    item = json.loads(json.dumps(configuration))
    item["id"] = name
    item["labels"] = dict(args[i+1].split("=", 1) for i, value in enumerate(args) if value == "--label")
    item["networks"] = [{"network": args[args.index("--network")+1].split(",")[0]}]
    item["publishedPorts"] = []
    item["resources"]["cpus"] = int(args[args.index("--cpus")+1])
    memory = args[args.index("--memory")+1]
    item["resources"]["memoryInBytes"] = int(memory[:-1]) * (1024 ** (3 if memory[-1] == "g" else 2))
    if name.startswith("mybox-helper-"):
        item["labels"] = {"io.github.janpmz.my-container-box.helper": "true"}
    state[name] = {"id": name, "configuration": item, "status": dict(status, state="stopped")}
    statepath.write_text(json.dumps(state))
elif args[0] in ("start", "stop"):
    state[args[1]]["status"]["state"] = "running" if args[0] == "start" else "stopped"
    statepath.write_text(json.dumps(state))
elif args[0] == "delete":
    state.pop(args[1], None)
    statepath.write_text(json.dumps(state))
elif args[:2] == ["network", "inspect"]:
    print(json.dumps([{"id": args[2], "configuration": {"mode": "nat", "labels": {"io.github.janpmz.my-container-box.managed": "true"}}, "status": {"ipv4Subnet": "192.168.65.0/24", "ipv4Gateway": "192.168.65.1", "ipv6Subnet": "fd00:1234::/64"}}]))
elif args[0] == "inspect":
    print(json.dumps([state[args[1]]]))
elif args[0] == "list":
    if scenario == "foreign-member":
        state["unrelated"] = {"id": "unrelated", "configuration": configuration, "status": {"state": "running"}}
    print(json.dumps(list(state.values())))
elif args[0] == "exec" and "--interactive" in args:
    print(json.dumps(args))
    sys.exit(int(os.environ.get("MYBOX_TEST_EXIT", "0")))
elif args[0] == "cp" and not args[1].startswith("mybox-"):
    payload = pathlib.Path(args[1])
    entries = sorted(str(p.relative_to(payload)) for p in payload.rglob("*")) if payload.is_dir() else ["<file>"]
    pathlib.Path(os.environ["MYBOX_TEST_PAYLOAD"]).write_text(json.dumps(entries))
'''

FAKE_HOST = r'''#!/usr/bin/python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["MYBOX_TEST_LOG"], "a") as log:
    log.write(json.dumps(["host"] + args) + "\n")
if args[0] == "/sbin/ifconfig":
    print("lo0: flags=0\n\tinet 127.0.0.1 netmask 0xff000000\nbridge999: flags=0\n\tinet 192.168.65.1 netmask 0xffffff00\n\tmember: vmenet999 flags=0")
elif args[0] == "/usr/bin/sudo":
    action = args[2]
    if os.environ.get("MYBOX_TEST_FIREWALL_FAIL") == action:
        print("Test firewall failure", file=sys.stderr)
        sys.exit(1)
    print("mybox-firewall-manager-5-ok")
else:
    sys.exit(2)
'''



class CLI(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mybox-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        backend = self.directory / "container"
        backend.write_text(FAKE_BACKEND)
        backend.chmod(0o755)
        host = self.directory / "host"
        host.write_text(FAKE_HOST)
        host.chmod(0o755)
        self.log = self.directory / "calls.jsonl"
        self.payload = self.directory / "payload.json"
        self.container_root = self.directory / "containers"
        self.environment = dict(os.environ, PATH=str(self.directory) + ":/usr/bin:/bin",
                                MYBOX_TEST_LOG=str(self.log), MYBOX_TEST_PAYLOAD=str(self.payload), MYBOX_TEST_HOST=str(host),
                                MYBOX_TEST_CONTAINER_ROOT=str(self.container_root),
                                MYBOX_TEST_PORT_ROOT=str(self.directory / "ports"), MYBOX_TEST_FAKE_FORWARDER="1")

    def run_box(self, *arguments, success=True, answers=None):
        result = subprocess.run([BOX, *arguments], env=self.environment, text=True,
                                input=answers, capture_output=True, timeout=15)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_help_does_not_need_backend(self):
        self.assertIn("whole folder", self.run_box("help").stdout)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.log.with_suffix(".service-log").exists())

    def test_first_service_command_starts_the_service_once(self):
        service = self.log.with_suffix(".service")
        service.write_text("unregistered")
        service_log = self.log.with_suffix(".service-log")
        self.run_box("_complete-boxes")
        self.assertFalse(service_log.exists())
        result = self.run_box("list")
        self.assertIn("Starting Apple's container service", result.stderr)
        self.assertIn("demo", result.stdout)
        self.run_box("enter", "demo", "--", "/bin/true")
        actions = [json.loads(line)[:2] for line in service_log.read_text().splitlines()]
        self.assertEqual(actions.count(["system", "start"]), 1)
        self.assertEqual(service.read_text(), "running")

    def test_service_errors_stop_the_requested_operation(self):
        service = self.log.with_suffix(".service")
        service.write_text("unknown")
        self.run_box("list", success=False)
        self.assertNotIn('"start"', self.log.with_suffix(".service-log").read_text())
        service.write_text("not running")
        self.environment["MYBOX_TEST_SERVICE_FAIL"] = "1"
        result = self.run_box("enter", "demo", success=False)
        self.assertIn("test service startup failure", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_version(self):
        self.assertEqual(self.run_box("--version").stdout.strip(), "MyBox 0.1.0")

    def test_list_shows_limits_and_sparse_disk_use_without_starting_boxes(self):
        disk = self.container_root / "mybox-demo" / "rootfs.ext4"
        disk.parent.mkdir(parents=True)
        with disk.open("wb") as stream:
            stream.truncate(8 * 1024 ** 3)
        self.assertLess(disk.stat().st_blocks * 512, disk.stat().st_size)
        for scenario, state in [("normal", "running"), ("stopped", "stopped")]:
            with self.subTest(state=state):
                self.environment["MYBOX_TEST_SCENARIO"] = scenario
                lines = self.run_box("list").stdout.splitlines()
                self.assertEqual(re.split(r"\s{2,}", lines[0]), ["NAME", "STATE", "CORES", "MEMORY", "DISK USED", "DISK MAX", "PORTS"])
                self.assertEqual(re.split(r"\s{2,}", lines[1].replace(",", ".")), ["demo", state, "2", "2 GB", "0 B", "8.6 GB", "-"])
                self.assertIn("DISK MAX is virtual capacity", "\n".join(lines))
                self.assertEqual(self.calls(), [["list", "--all", "--format", "json"]])
                self.log.unlink()

    def test_list_reports_allocated_disk_space_after_writes(self):
        disk = self.container_root / "mybox-demo" / "rootfs.ext4"
        disk.parent.mkdir(parents=True)
        with disk.open("wb") as stream:
            stream.write(os.urandom(1024 ** 2))
            stream.truncate(8 * 1024 ** 3)
        row = re.split(r"\s{2,}", self.run_box("list").stdout.splitlines()[1].replace(",", "."))
        value, unit = row[4].split()
        self.assertEqual(unit, "MB")
        self.assertGreater(float(value), 0)
        self.assertAlmostEqual(float(value), disk.stat().st_blocks * 512 / 1_000_000, delta=0.05)
        self.assertEqual(row[5], "8.6 GB")
        self.assertEqual(self.calls(), [["list", "--all", "--format", "json"]])

    def test_list_uses_each_box_configuration_and_preserves_ports(self):
        for memory, expected in [(512 * 1024 ** 2, "0.5 GB"), (1536 * 1024 ** 2, "1.5 GB")]:
            with self.subTest(memory=memory):
                configuration = {
                    "labels": {"io.github.janpmz.my-container-box.managed": "true"},
                    "resources": {"cpus": 6, "memoryInBytes": memory},
                    "publishedPorts": [{"hostPort": 8766, "containerPort": 8080}],
                }
                self.log.with_suffix(".state").write_text(json.dumps({"mybox-demo": {
                    "id": "mybox-demo", "configuration": configuration, "status": {"state": "stopped"}
                }}))
                row = re.split(r"\s{2,}", self.run_box("list").stdout.splitlines()[1].replace(",", "."))
                self.assertEqual(row, ["demo", "stopped", "6", expected, "-", "-", "inside 8080 / outside 8766 (localhost)"])

    def test_list_shows_unknown_resources_without_inventing_defaults(self):
        self.log.with_suffix(".state").write_text(json.dumps({"mybox-demo": {
            "id": "mybox-demo", "configuration": {"labels": {"io.github.janpmz.my-container-box.managed": "true"}},
            "status": {"state": "stopped"}
        }}))
        row = re.split(r"\s{2,}", self.run_box("list").stdout.splitlines()[1])
        self.assertEqual(row, ["demo", "stopped", "-", "-", "-", "-", "-"])
        disk = self.container_root / "mybox-demo" / "rootfs.ext4"
        disk.parent.mkdir(parents=True)
        disk.symlink_to(self.directory / "calls.state")
        self.assertEqual(re.split(r"\s{2,}", self.run_box("list").stdout.splitlines()[1]), row)

    def test_invalid_input_never_reaches_backend(self):
        cases = [
            ("create", "../escape"), ("stop", "--all"), ("create", "Uppercase"), ("create", "demo\n"),
            ("create", "demo-"),
            ("create", "demo", "--port", "0.0.0.0:80:8080"),
            ("create", "demo", "--port", "8766:8080"),
            ("create", "demo", "--inside-port", "0", "--outside-port", "8766"),
            ("create", "demo", "--inside-port", "65536", "--outside-port", "8766"),
            ("create", "demo", "--inside-port", "8080", "--outside-port", "0"),
            ("create", "demo", "--inside-port", "8080", "--outside-port", "65536"),
            ("create", "demo", "--inside-port", "8080", "--outside-port", "0.0.0.0:8766"),
            ("create", "demo", "--inside-port", "8080"),
            ("create", "demo", "--outside-port", "8766"),
            ("create", "demo", "--inside-port"),
            ("create", "demo", "--outside-port"),
            ("create", "demo", "--inside-port", "8080", "--inside-port", "8081", "--outside-port", "8766"),
            ("create", "demo", "--inside-port", "8080", "--outside-port", "8766", "--inside-port", "8081", "--outside-port", "8766"),
            ("create", "demo", "--cpus", "0"),
            ("create", "demo", "--memory", "lots"),
            ("create", "demo", "--volume", "/Users:/host"),
            ("enter", "demo", "ls"), ("enter", "demo", "--"),
            ("export", "demo", "/etc/passwd", "/tmp/result"),
            ("export", "demo", "../etc", "/tmp/result"),
            ("export", "demo", "/workspace/../etc", "/tmp/result"),
            ("list", "--all"), ("delete", "demo"),
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.run_box(*arguments, success=False)
        self.assertEqual(self.calls(), [])

    def test_create_publishes_localhost_only(self):
        result = self.run_box("create", "demo", "--inside-port", "8080", "--outside-port", "8766",
                     "--inside-port", "8081", "--outside-port", "8767",
                     "--cpus", "3", "--memory", "1g")
        create = next(call for call in self.calls() if call[0] == "create" and call[call.index("--name") + 1] == "mybox-demo")
        self.assertEqual(create[0], "create")
        self.assertNotIn("--publish", create)
        saved = json.loads((self.directory / "ports/mybox-demo/ports.json").read_text())
        self.assertEqual(saved["mappings"], [{"inside": 8080, "outside": 8766}, {"inside": 8081, "outside": 8767}])
        self.assertEqual(create[create.index("--cpus") + 1], "3")
        self.assertEqual(create[create.index("--memory") + 1], "1g")
        self.assertEqual(create[create.index("--name") + 1], "mybox-demo")
        self.assertEqual(create[create.index("--network") + 1], "mybox-net-demo,alias=demo.box.test")
        self.assertIn("Inside port:  8080 (Linux box)", result.stdout)
        self.assertIn("Outside port: 8766 (localhost only)", result.stdout)
        self.assertNotIn("http://", result.stdout)
        self.assertNotIn("https://", result.stdout)
        for forbidden in ["--volume", "--mount", "--ssh", "--publish-socket", "--env-file", "--rm"]:
            self.assertNotIn(forbidden, create)

    def test_port_flags_can_come_in_either_order(self):
        self.run_box("create", "demo", "--outside-port", "8766", "--inside-port", "8080")
        create = next(call for call in self.calls() if call[0] == "create" and call[call.index("--name") + 1] == "mybox-demo")
        self.assertNotIn("--publish", create)
        saved = json.loads((self.directory / "ports/mybox-demo/ports.json").read_text())
        self.assertEqual(saved["mappings"], [{"inside": 8080, "outside": 8766}])

    def test_create_without_ports_publishes_nothing(self):
        self.run_box("create", "demo")
        self.assertTrue(all("--publish" not in call for call in self.calls() if call[0] == "create"))

    def test_helper_and_verified_firewall_precede_first_workload(self):
        self.run_box("create", "demo")
        calls = self.calls()
        helper_create = next(c for c in calls if c[0] == "create" and "mybox-demo" not in c)
        helper = helper_create[helper_create.index("--name") + 1]
        real_start = calls.index(["start", "mybox-demo"])
        helper_start = calls.index(["start", helper])
        apply = next(i for i, c in enumerate(calls) if c[:2] == ["host", "/usr/bin/sudo"] and c[3] == "apply")
        verifies = [i for i, c in enumerate(calls) if c[:2] == ["host", "/usr/bin/sudo"] and c[3] == "verify"]
        self.assertLess(helper_start, apply)
        self.assertLess(apply, verifies[0])
        self.assertLess(verifies[0], real_start)
        self.assertLess(real_start, verifies[1])
        self.assertLess(verifies[1], calls.index(["stop", helper]))
        self.assertIn(["delete", helper], calls)
        self.assertIn("--read-only", helper_create)
        self.assertNotIn("--publish", helper_create)
        self.assertNotIn("--volume", helper_create)

    def test_failed_firewall_never_starts_new_box_and_removes_helper(self):
        for failure in ("apply", "verify"):
            with self.subTest(failure=failure):
                self.environment["MYBOX_TEST_FIREWALL_FAIL"] = failure
                self.run_box("create", "demo", success=False)
                calls = self.calls()
                self.assertNotIn(["start", "mybox-demo"], calls)
                self.assertFalse(any(c[0] == "exec" for c in calls))
                helpers = [c[c.index("--name") + 1] for c in calls if c[0] == "create" and "mybox-demo" not in c]
                self.assertTrue(helpers)
                for helper in helpers:
                    self.assertIn(["stop", helper], calls)
                    self.assertIn(["delete", helper], calls)

    def test_automatic_starts_cannot_bypass_failed_firewall(self):
        source = self.directory / "input.txt"
        source.write_text("test")
        self.environment.update(MYBOX_TEST_SCENARIO="stopped", MYBOX_TEST_FIREWALL_FAIL="apply")
        for arguments in [("start", "demo"), ("enter", "demo"),
                          ("import", "demo", str(source)),
                          ("export", "demo", "file", str(self.directory / "output"))]:
            with self.subTest(arguments=arguments):
                self.run_box(*arguments, success=False)
        self.assertNotIn(["start", "mybox-demo"], self.calls())
        self.assertFalse(any(c[0] in ("exec", "cp") for c in self.calls()))

    def test_running_box_is_checked_before_enter(self):
        self.environment["MYBOX_TEST_FIREWALL_FAIL"] = "verify"
        self.run_box("enter", "demo", success=False)
        self.assertFalse(any(c[0] == "exec" for c in self.calls()))

    def test_shared_or_foreign_network_refused_without_guest_execution(self):
        for scenario in ("legacy", "foreign-member"):
            with self.subTest(scenario=scenario):
                self.environment["MYBOX_TEST_SCENARIO"] = scenario
                self.run_box("start", "demo", success=False)
        self.assertFalse(any(c[0] in ("start", "exec", "delete") for c in self.calls()))

    def test_refuses_unmanaged_or_unsafe_boxes(self):
        for scenario in ["unmanaged", "mount", "ssh", "public-port"]:
            with self.subTest(scenario=scenario):
                self.environment["MYBOX_TEST_SCENARIO"] = scenario
                self.run_box("stop", "demo", success=False)
        self.assertTrue(all(call[0] == "inspect" for call in self.calls()))

    def test_enter_preserves_argument_boundaries_and_exit_status(self):
        argument = "literal ' ; $(touch /tmp/do-not-run)"
        result = self.run_box("enter", "demo", "--", "/bin/echo", argument)
        call = json.loads(result.stdout)
        self.assertEqual(call[-2:], ["/bin/echo", argument])
        self.assertIn("--interactive", call)
        self.assertNotIn("--tty", call)
        self.environment["MYBOX_TEST_EXIT"] = "37"
        self.assertEqual(self.run_box("enter", "demo", "--", "/bin/false", success=False).returncode, 37)

    def test_import_includes_all_names_without_following_links(self):
        source = self.directory / "sample project"
        source.mkdir()
        directories = ["nested", "node_modules", ".venv", ".git", ".ssh", ".aws"]
        filenames = [".env", ".env.local", ".env.example", ".npmrc", "private.pem", "private.key",
                     "identity.p12", "identity.pfx", "id_rsa", "id_ed25519", ".DS_Store", "file.txt", ".hidden"]
        for name in directories:
            (source / name).mkdir()
            (source / name / "keep.txt").write_text("dummy")
        for name in filenames:
            (source / name).write_text("dummy")
        (source / "outside-link").symlink_to(self.directory, target_is_directory=True)
        os.mkfifo(source / "fifo")
        result = self.run_box("import", "demo", str(source))
        entries = json.loads(self.payload.read_text())
        self.assertEqual(entries, sorted(filenames + directories + [name + "/keep.txt" for name in directories]))
        self.assertIn("Skipped 2 symlinks or special files.", result.stdout)

    def test_import_accepts_selected_env_file_and_dependency_folder(self):
        source_file = self.directory / ".env"
        source_file.write_text("DUMMY_SECRET=test-value\n")
        source_folder = self.directory / "node_modules"
        source_folder.mkdir()
        (source_folder / "package.txt").write_text("dummy")
        for source, expected in [(source_file, ["<file>"]), (source_folder, ["package.txt"])]:
            with self.subTest(source=source.name):
                result = self.run_box("import", "demo", str(source))
                self.assertEqual(json.loads(self.payload.read_text()), expected)
                self.assertIn("Imported to demo:/workspace/" + source.name, result.stdout)
                self.assertNotIn("Skipped", result.stdout)

    def test_export_cannot_replace_existing_path_or_dangling_link(self):
        destination = self.directory / "existing"
        destination.write_text("original")
        self.run_box("export", "demo", "file", str(destination), success=False)
        self.assertEqual(destination.read_text(), "original")
        link = self.directory / "dangling"
        link.symlink_to(self.directory / "missing")
        self.run_box("export", "demo", "file", str(link), success=False)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
