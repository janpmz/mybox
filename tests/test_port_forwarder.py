"""Exercise the real Swift TCP helper with loopback peers and a fake box identity."""
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
import uuid

import test_cli

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / ".build/mybox-port-forwarder"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def receive_exact(client, count):
    result = bytearray()
    while len(result) < count:
        data = client.recv(count - len(result))
        if not data:
            raise AssertionError("The connection closed before the response finished.")
        result.extend(data)
    return bytes(result)


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            while data := self.request.recv(65536):
                if data == b"test-server-half-close":
                    self.request.sendall(b"ready")
                    self.request.shutdown(socket.SHUT_WR)
                    self.server.received_after_eof = bytearray()
                    while data := self.request.recv(65536):
                        self.server.received_after_eof.extend(data)
                    self.server.received_event.set()
                    return
                self.request.sendall(data)
            self.request.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class EchoServer(socketserver.ThreadingTCPServer):
    daemon_threads = True


class Forwarder(unittest.TestCase):
    target_address = "127.0.0.1"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mybox-forwarder-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "mybox-test"
        self.root.mkdir(mode=0o700)
        self.server = EchoServer(("127.0.0.1", 0), Echo)
        self.server.received_event = threading.Event()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.inside = self.server.server_address[1]
        self.outside = free_port()
        self.config = dict(version=1, id="mybox-test", created="created-test",
                           mappings=[dict(inside=self.inside, outside=self.outside)],
                           target=dict(address=self.target_address, started="boot-test"), revision=str(uuid.uuid4()))
        self.identity = [{"id": "mybox-test", "configuration": {
            "creationDate": "created-test", "labels": {"com.mybox.managed": "true", "com.mybox.network": "mybox-net-test"},
            "mounts": [], "publishedSockets": [], "publishedPorts": [], "ssh": False,
            "networks": [{"network": "mybox-net-test"}]},
                          "status": {"state": "running", "startedDate": "boot-test", "networks": [{"network": "mybox-net-test", "ipv4Address": self.target_address + "/24"}]}}]
        (self.root / "identity.json").write_text(json.dumps(self.identity))
        self.backend = self.root / "container"
        self.backend.write_text('''#!/usr/bin/python3
import json, os, pathlib, sys
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
if args == ["inspect", "mybox-test"]:
    print((root / "identity.json").read_text())
elif args == ["list", "--all", "--format", "json"]:
    print((root / "identity.json").read_text())
elif args == ["network", "inspect", "mybox-net-test"]:
    print(json.dumps([{"id": "mybox-net-test", "configuration": {"mode": "nat", "labels": {"com.mybox.managed": "true"}},
                      "status": {"ipv4Subnet": "192.168.65.0/24", "ipv4Gateway": "192.168.65.1", "ipv6Subnet": "fd00:1234::/64"}}]))
elif args[:6] == ["exec", "--interactive", "--user", "0", "mybox-test", "/usr/bin/perl"]:
    (root / ("relay-" + str(os.getpid()))).write_text(json.dumps(args[:6] + [args[-1]]))
    os.execv("/usr/bin/perl", args[5:])
else:
    sys.exit(9)
''')
        self.backend.chmod(0o700)
        self.write()
        self.log = (self.root / "log").open("wb")
        self.addCleanup(self.log.close)
        self.process = subprocess.Popen([HELPER, self.root, self.backend], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=self.log, env=dict(os.environ, MYBOX_TRACE="1"))
        self.addCleanup(self.stop)
        self.wait_status()

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=5)

    def write(self):
        self.config["revision"] = str(uuid.uuid4())
        temporary = self.root / "next.json"
        temporary.write_text(json.dumps(self.config))
        temporary.replace(self.root / "ports.json")

    def wait_status(self, success=True):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                status = json.loads((self.root / "status.json").read_text())
                if status["revision"] == self.config["revision"]:
                    self.assertEqual(status.get("error") is None, success, status)
                    return status
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            if self.process.poll() is not None:
                self.fail("Helper exited: " + (self.root / "log").read_text())
            time.sleep(.03)
        self.fail("No forwarding acknowledgement: " + (self.root / "log").read_text())

    def connect(self, port=None):
        return socket.create_connection(("127.0.0.1", port or self.outside), timeout=5)

    def test_large_bidirectional_transfer_and_half_close(self):
        payload = bytes(range(256)) * 16384
        with self.connect() as client:
            errors = []
            def send():
                try:
                    client.sendall(payload)
                    client.shutdown(socket.SHUT_WR)
                except OSError as error:
                    errors.append(error)
            thread = threading.Thread(target=send)
            thread.start()
            received = bytearray()
            while data := client.recv(65536):
                received.extend(data)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(received), len(payload), (self.root / "log").read_text())
            self.assertEqual(bytes(received), payload)

    def test_add_and_remove_preserve_an_unrelated_tcp_connection(self):
        with self.connect() as existing:
            second = free_port()
            self.config["mappings"].append(dict(inside=self.inside, outside=second))
            self.write()
            self.wait_status()
            with self.connect(second) as removed:
                removed.sendall(b"second")
                self.assertEqual(removed.recv(6), b"second")
                self.config["mappings"].pop()
                self.write()
                self.wait_status()
                self.assertEqual(removed.recv(1), b"")
            existing.sendall(b"still-running")
            self.assertEqual(existing.recv(13), b"still-running")
            with self.assertRaises(OSError):
                self.connect(second)

    def test_server_half_close_still_accepts_client_data(self):
        with self.connect() as client:
            client.sendall(b"test-server-half-close")
            response = bytearray()
            while data := client.recv(65536):
                response.extend(data)
            self.assertEqual(response, b"ready")
            payload = bytes(range(256)) * 1024
            client.sendall(payload)
            client.shutdown(socket.SHUT_WR)
            self.assertTrue(self.server.received_event.wait(5))
            self.assertEqual(self.server.received_after_eof, payload)

    def test_port_conflict_preserves_existing_connections_and_recovers(self):
        with self.connect() as existing, socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            self.config["mappings"].append(dict(inside=self.inside, outside=occupied.getsockname()[1]))
            self.write()
            self.wait_status(success=False)
            existing.sendall(b"kept")
            self.assertEqual(existing.recv(4), b"kept")
            self.config["mappings"].pop()
            self.write()
            self.wait_status()
            existing.sendall(b"again")
            self.assertEqual(existing.recv(5), b"again")

    def test_helper_exits_when_the_box_identity_changes(self):
        self.identity[0]["status"]["startedDate"] = "different-boot"
        (self.root / "identity.json").write_text(json.dumps(self.identity))
        self.assertEqual(self.process.wait(timeout=6), 1)
        with self.assertRaises(OSError):
            self.connect()

    def test_empty_mapping_closes_connections_and_exits(self):
        with self.connect() as client:
            self.config["mappings"] = []
            self.write()
            self.assertFalse(self.wait_status()["active"])
            self.assertEqual(self.process.wait(timeout=5), 0)
            self.assertEqual(client.recv(1), b"")

    def test_second_helper_cannot_replace_the_running_helper(self):
        second = subprocess.run([HELPER, self.root, self.backend], capture_output=True, timeout=5)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIsNone(self.process.poll())
        with self.connect() as client:
            client.sendall(b"one")
            self.assertEqual(client.recv(3), b"one")

    def test_port_command_updates_an_older_helper_without_restarting_the_box(self):
        path = self.root / "status.json"
        status = json.loads(path.read_text())
        status.pop("protocolVersion", None)
        path.write_text(json.dumps(status))
        host = self.root / "fake-host"
        host.write_text(test_cli.FAKE_HOST)
        host.chmod(0o700)
        environment = dict(os.environ, PATH=str(self.root) + ":/usr/bin:/bin",
                           MYBOX_TEST_PORT_ROOT=str(self.root.parent), MYBOX_TEST_HOST=str(host),
                           MYBOX_TEST_LOG=str(self.root / "commands.jsonl"))
        second = free_port()
        result = subprocess.run([test_cli.BOX, "ports", "add", "test", "--inside-port", str(self.inside),
                                 "--outside-port", str(second)], env=environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Updating port forwarding support", result.stderr)
        self.assertEqual(self.process.wait(timeout=5), 0)
        status = json.loads(path.read_text())
        self.assertEqual(status["protocolVersion"], 2)
        self.assertNotEqual(status["pid"], self.process.pid)
        self.addCleanup(lambda: os.kill(status["pid"], 15))
        for port in (self.outside, second):
            with self.connect(port) as client:
                client.sendall(b"updated")
                self.assertEqual(receive_exact(client, 7), b"updated")
        self.assertEqual(json.loads((self.root / "identity.json").read_text()), self.identity)


class LoopbackForwarder(Forwarder):
    # The server binds only to .1, so the initial connection to .2 must fail.
    # The fake backend executes the actual guest relay program with local Perl.
    target_address = "127.0.0.2"

    def wait_for_relays_to_exit(self):
        paths = list(self.root.glob("relay-*"))
        self.assertTrue(paths, "The test did not use the guest loopback relay.")
        deadline = time.monotonic() + 5
        while True:
            alive = []
            for path in paths:
                pid = int(path.name.removeprefix("relay-"))
                try:
                    os.kill(pid, 0)
                    alive.append(pid)
                except ProcessLookupError:
                    pass
            if not alive:
                return
            if time.monotonic() >= deadline:
                self.fail(f"Guest relay processes remain: {alive}")
            time.sleep(.05)

    def test_removing_mapping_cancels_guest_process(self):
        with self.connect() as client:
            client.sendall(b"ready")
            self.assertEqual(client.recv(5), b"ready")
            self.config["mappings"] = []
            self.write()
            self.wait_status()
            self.assertEqual(client.recv(1), b"")
            self.wait_for_relays_to_exit()

    def test_helper_exit_cancels_guest_process(self):
        with self.connect() as client:
            client.sendall(b"ready")
            self.assertEqual(client.recv(5), b"ready")
            self.stop()
            self.wait_for_relays_to_exit()

    def test_missing_server_closes_connection_and_keeps_helper_available(self):
        self.config["mappings"][0]["inside"] = free_port()
        self.outside = free_port()
        self.config["mappings"][0]["outside"] = self.outside
        self.write()
        self.wait_status()
        with self.connect() as client:
            self.assertEqual(client.recv(1), b"")
        self.assertIsNone(self.process.poll())
        self.wait_for_relays_to_exit()

    def test_large_response_to_slow_client_does_not_block_other_connections(self):
        with self.connect() as slow:
            slow.sendall(b"ready")
            self.assertEqual(slow.recv(5), b"ready")
            slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            slow.settimeout(.2)
            try:
                slow.sendall(b"x" * (8 * 1024 * 1024))
            except socket.timeout:
                pass
            with self.connect() as other:
                other.sendall(b"independent")
                self.assertEqual(other.recv(11), b"independent")


if __name__ == "__main__":
    unittest.main()
