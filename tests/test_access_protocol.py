"""Exercise the production Swift approval connection and scope checks without root or PF."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'service'))
import access
from broker import packet, receive


class AccessProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='mybox-access-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.program = cls.directory / 'check'
        harness = cls.directory / 'Main.swift'
        harness.write_text('''
import Darwin
import Foundation
@main struct Check {
    static func main() {
        do {
            let args = Array(CommandLine.arguments.dropFirst())
            if args[0] == "scope" {
                let scope = try JSONDecoder().decode(ServiceAccessScope.self, from: Data(args[1].utf8))
                let request = try JSONDecoder().decode(ServiceFirewallRequest.self, from: Data(args[2].utf8))
                try scope.validate(request, owner: 499)
            } else {
                let approval = try BoxAccessApproval(["enter", "demo", "/bin/pwd"],
                    socketPath: args[1], expectedOwner: UInt32(args[2])!)
                if args[0] == "exec" {
                    let strings: [String] = ["/bin/sleep", "2"]
                    let values = strings.map { strdup($0) } + [nil]
                    _ = values.withUnsafeBufferPointer { execv("/bin/sleep", $0.baseAddress!) }
                    throw BoxError("exec failed")
                }
                try approval.firewall("11111111-1111-4111-8111-111111111111")
                try approval.finish()
                if args[0] == "reuse" { try approval.firewall("22222222-1111-4111-8111-111111111111") }
            }
            print("ready")
        } catch { note(String(describing: error)); exit(1) }
    }
}
''')
        sources = ['Support.swift', 'HostProcess.swift', 'NetworkPolicy.swift',
                   'ServiceFirewall.swift', 'BoxAccess.swift']
        compiled = subprocess.run(['xcrun', 'swiftc', '-module-cache-path', str(ROOT / '.build/module-cache'),
                        *[str(ROOT / 'Sources' / source) for source in sources], str(harness),
                        '-o', str(cls.program)], capture_output=True, text=True, timeout=120)
        if compiled.returncode:
            raise RuntimeError(compiled.stderr)

    def setUp(self):
        self.policy = dict(owner=499, network='mybox-net-demo', subnet4='192.168.65.0/24',
                           subnet6='fd00::/64', gateway='192.168.65.1', ports=[], active=True)

    def scope_check(self, request, **changes):
        scope = dict(box='demo', expires=time.time() + 60, actions=['apply', 'verify', 'update-ports'], policy=None)
        scope.update(changes)
        return subprocess.run([self.program, 'scope', json.dumps(scope), json.dumps(request)],
                              capture_output=True, text=True, timeout=10)

    def test_helper_allows_only_the_approved_box_policy_action_and_lifetime(self):
        for action in ['apply', 'verify', 'update-ports']:
            response = self.scope_check(dict(action=action, policy=self.policy), policy=self.policy)
            self.assertEqual(response.returncode, 0, response.stderr)
        cases = [
            (dict(action='remove', policy=self.policy), dict(actions=['remove'])),
            (dict(action='install', policy=self.policy), dict(actions=['install'])),
            (dict(action='apply', policy=self.policy), dict(actions=['verify'])),
            (dict(action='verify', policy=self.policy), dict(expires=time.time() - 1)),
            (dict(action='verify', policy=self.policy), dict(expires=time.time() + 600)),
            (dict(action='verify', policy=self.policy), dict(box='other')),
            (dict(action='verify', policy=self.policy), dict(box='demo\n')),
            (dict(action='verify', policy=self.policy), dict(policy=dict(self.policy, ports=[8080]))),
        ]
        for field, value in [('owner', 501), ('network', 'mybox-net-other'), ('active', False),
                             ('subnet4', '8.8.8.0/24'), ('ports', [0])]:
            cases.append((dict(action='verify', policy=dict(self.policy, **{field: value})), {}))
        for request, changes in cases:
            with self.subTest(request=request, changes=changes):
                self.assertNotEqual(self.scope_check(request, **changes).returncode, 0)

    def exchange(self, handler, mode='normal', owner=None):
        path = str(self.directory / 'socket')
        Path(path).unlink(missing_ok=True)
        errors = []
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(path)
            listener.listen()
            listener.settimeout(10)
            def server():
                try:
                    conn, _ = listener.accept()
                    with conn:
                        conn.settimeout(5)
                        handler(conn)
                except Exception as error:
                    errors.append(error)
            thread = threading.Thread(target=server)
            thread.start()
            response = subprocess.run([self.program, mode, path, str(os.getuid() if owner is None else owner)],
                                      capture_output=True, text=True, timeout=15)
            thread.join(6)
            self.assertFalse(thread.is_alive(), 'Approval connection remained open.')
            if errors:
                raise errors[0]
            return response

    def test_swift_worker_and_python_service_share_one_authentication(self):
        calls = []
        install = self.directory / 'install'
        install.mkdir(exist_ok=True)
        (install / 'owner').write_text('501')

        def helper(command, **kwargs):
            calls.append(command)
            if command[0] == '/bin/launchctl':
                challenge, reason = json.loads(kwargs['input'])
                self.assertIn('demo', reason)
                self.assertIn('firewall', reason)
                return SimpleNamespace(returncode=0, stdout=challenge.encode())
            self.assertEqual(command, [access.FIREWALL, 'approve-access', '11111111-1111-4111-8111-111111111111'])
            return SimpleNamespace(stdout=json.dumps(dict(action='verify', policy=self.policy)).encode())

        # Patch only the service thread's dependencies, not subprocess.run used
        # to launch the real Swift client in exchange().
        real_run = subprocess.run
        def run(command, **kwargs):
            return real_run(command, **kwargs) if command[0] == self.program else helper(command, **kwargs)

        with patch.object(access, 'INSTALL', install), patch.object(access.os, 'geteuid', return_value=0), \
             patch.object(access, 'peer_uid', return_value=499), \
             patch.object(access.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=499)), \
             patch.object(access.os, 'stat', return_value=SimpleNamespace(st_uid=501)), \
             patch.object(access.subprocess, 'run', side_effect=run):
            response = self.exchange(access.serve)
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(len(calls), 2, calls)

    def test_swift_rejects_cancellation_replayed_approval_and_wrong_service_owner(self):
        for reply in [['denied'], ['approved', 'old-challenge']]:
            def deny(conn):
                receive(conn)
                packet(conn, reply)
                self.assertEqual(conn.recv(1), b'')
            with self.subTest(reply=reply):
                self.assertNotEqual(self.exchange(deny).returncode, 0)
        self.assertNotEqual(self.exchange(lambda conn: self.assertEqual(conn.recv(1), b''),
                                         owner=os.getuid() + 1).returncode, 0)

    def test_swift_rejects_wrong_firewall_reply_and_closes_finished_approval(self):
        for mode in ['bad-firewall', 'normal', 'reuse']:
            def server(conn):
                version, challenge, args = receive(conn)
                self.assertEqual((version, args), ('access-v2', ['enter', 'demo', '/bin/pwd']))
                packet(conn, ['approved', challenge])
                request = receive(conn)
                packet(conn, ['firewall', 'wrong-token'] if mode == 'bad-firewall' else request)
                if mode != 'bad-firewall':
                    self.assertEqual(receive(conn), ['finish'])
                    packet(conn, ['finished'])
                self.assertEqual(conn.recv(1), b'')
            with self.subTest(mode=mode):
                response = self.exchange(server, mode=mode)
                self.assertEqual(response.returncode == 0, mode == 'normal', response.stderr)

    def test_approval_connection_does_not_survive_exec(self):
        def server(conn):
            _, challenge, _ = receive(conn)
            packet(conn, ['approved', challenge])
            # sleep runs for two seconds, but exec must close the socket now.
            conn.settimeout(1)
            self.assertEqual(conn.recv(1), b'')
        self.assertEqual(self.exchange(server, mode='exec').returncode, 0)

    def test_firewall_error_reaches_terminal_without_becoming_an_approval(self):
        def server(conn):
            _, challenge, _ = receive(conn)
            packet(conn, ['approved', challenge])
            _, token = receive(conn)
            packet(conn, ['firewall-error', token, 'The Mac network bridge for mybox-net-demo is missing.'])
            self.assertEqual(conn.recv(1), b'')
        response = self.exchange(server)
        self.assertNotEqual(response.returncode, 0)
        self.assertIn('network bridge for mybox-net-demo is missing', response.stderr)
        self.assertNotIn('approval', response.stderr)
