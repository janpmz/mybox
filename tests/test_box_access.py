"""Check the private service's one-use authentication protocol without displaying UI."""
from pathlib import Path
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import access
from broker import packet, receive


class BoxAccess(unittest.TestCase):
    def test_prompt_describes_what_the_requester_receives(self):
        self.assertIn('shell', access.prompt_for(['enter', 'demo']))
        reason = access.prompt_for(['enter', 'demo', 'cat', 'a b', 'a\nb', '\u202etest'])
        self.assertIn('receives output', reason)
        self.assertIn('"a b" "a\\nb" "\\u202etest"', reason)
        self.assertIn('copy of', access.prompt_for(['export', 'demo', '/workspace/x']))
        self.assertIn('write', access.prompt_for(['import', 'demo', 'file']))
        for args in (['enter', 'fake\nname'], ['shell', 'demo'], ['export', 'demo']):
            with self.assertRaises(ValueError):
                access.prompt_for(args)

    def test_missing_authentication_replayed_approval_and_fresh_approval(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as listener:
            path = str(Path(directory) / 'auth')
            listener.bind(path)
            listener.listen()
            challenges = []
            def server():
                for index in range(4):
                    conn, _ = listener.accept()
                    with conn:
                        version, challenge, args = receive(conn)
                        if version != 'access-v2':
                            return
                        challenges.append(challenge)
                        if args != ['enter', 'demo', 'pwd']:
                            return
                        answers = [['denied'], ['approved', challenges[0]], ['approved', challenge], ['approved', True]]
                        packet(conn, answers[index])
            thread = threading.Thread(target=server, daemon=True)
            thread.start()
            with patch.object(access, 'SOCKET', path), patch.object(access, 'peer_uid', return_value=0):
                for allowed in (False, False, True, False):
                    if allowed:
                        access.request(['enter', 'demo', 'pwd'])
                    else:
                        with self.assertRaises(PermissionError):
                            access.request(['enter', 'demo', 'pwd'])
            thread.join(3)
            self.assertEqual(len(set(challenges)), 4)

    def test_service_rejects_canceled_authentication_and_stale_helper_output(self):
        challenge = 'a' * 64
        with tempfile.TemporaryDirectory() as directory:
            install = Path(directory)
            (install / 'owner').write_text('501')
            for status, output, allowed in [(1, challenge, False), (0, 'old', False), (0, challenge, True)]:
                conn, caller = socket.socketpair()
                with self.subTest(status=status, output=output), conn, caller, \
                     patch.object(access, 'INSTALL', install), patch.object(os, 'geteuid', return_value=0), \
                     patch.object(access, 'peer_uid', return_value=499), \
                     patch.object(access.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=499)), \
                     patch.object(os, 'stat', return_value=SimpleNamespace(st_uid=501)), \
                     patch.object(access.subprocess, 'run', return_value=SimpleNamespace(
                         returncode=status, stdout=output.encode())) as authenticate:
                    packet(caller, ['access-v2', challenge, ['enter', 'demo', 'pwd']])
                    caller.shutdown(socket.SHUT_WR)
                    if allowed:
                        access.serve(conn)
                        self.assertEqual(receive(caller), ['approved', challenge])
                    else:
                        with self.assertRaises(PermissionError):
                            access.serve(conn)
                    self.assertIn(b'pwd', authenticate.call_args.kwargs['input'])

    def test_frontend_process_cannot_request_or_forge_approval(self):
        conn, other = socket.socketpair()
        with conn, other, patch.object(os, 'geteuid', return_value=0), \
             patch.object(access, 'peer_uid', return_value=123456), \
             patch.object(access.pwd, 'getpwnam') as private, patch.object(access.subprocess, 'run') as prompt:
            private.return_value.pw_uid = 499
            with self.assertRaises(PermissionError):
                access.serve(conn)
            prompt.assert_not_called()


class ApprovedPreparation(unittest.TestCase):
    def setUp(self):
        self.policy = dict(owner=499, network='mybox-net-demo', subnet4='192.168.65.0/24',
                           subnet6='fd00::/64', gateway='192.168.65.1', ports=[], active=True)
        self.tokens = [f'{index:08d}-1111-4111-8111-111111111111' for index in range(1, 5)]

    def run_preparation(self, actions, messages, effect=None):
        self.calls = []

        def helper(command, **kwargs):
            scope = json.loads(kwargs['input'])
            index = len(self.calls)
            self.assertEqual(command, [access.FIREWALL, 'approve-access', self.tokens[index]])
            self.assertEqual(scope['box'], 'demo')
            self.assertEqual(scope['policy'], self.policy if index else None)
            self.assertGreater(scope['expires'], time.time())
            self.assertLessEqual(kwargs['timeout'], access.PREPARATION_SECONDS)
            self.assertTrue(kwargs['check'])
            self.calls.append(scope)
            if effect:
                return effect(index)
            self.assertIn(actions[index], scope['actions'])
            return SimpleNamespace(stdout=json.dumps(dict(action=actions[index], policy=self.policy)).encode())

        conn, caller = socket.socketpair()
        with conn, caller, patch.object(access.subprocess, 'run', side_effect=helper):
            for message in messages:
                packet(caller, message)
            caller.shutdown(socket.SHUT_WR)
            access.prepare_box(conn, 'demo')
            conn.shutdown(socket.SHUT_WR)
            answers = []
            try:
                while True:
                    answers.append(receive(caller))
            except EOFError:
                return answers

    def test_running_stopped_and_recovered_boxes_finish_without_sudo(self):
        for actions in [('verify',), ('apply', 'verify', 'verify'), ('update-ports', 'verify')]:
            with self.subTest(actions=actions):
                requests = [['firewall', token] for token in self.tokens[:len(actions)]]
                self.assertEqual(self.run_preparation(actions, requests + [['finish']]),
                                 requests + [['finished']])

    def test_cannot_repeat_requests_change_operations_or_finish_before_checks(self):
        for actions, messages in [
            ((), [['finish']]),
            ((), [['firewall', '../request']]),
            ((), [['enter', 'other-box']]),
            (('apply',), [['firewall', self.tokens[0]], ['firewall', self.tokens[0]]]),
            (('apply',), [['firewall', self.tokens[0]], ['finish']]),
            (('verify',), [['firewall', self.tokens[0]], ['firewall', self.tokens[1]]]),
        ]:
            with self.subTest(messages=messages), self.assertRaises((ValueError, PermissionError)):
                self.run_preparation(actions, messages)

    def test_firewall_failure_cannot_confirm_access(self):
        def failure(_):
            raise access.subprocess.CalledProcessError(1, access.FIREWALL, stderr=b'The network bridge is missing.')
        self.assertEqual(self.run_preparation(('verify',), [['firewall', self.tokens[0]]], failure),
                         [['firewall-error', self.tokens[0], 'The network bridge is missing.']])
        self.assertEqual(len(self.calls), 1)

    def test_firewall_timeout_and_terminal_controls_are_reported_without_approval(self):
        def timeout(_):
            raise access.subprocess.TimeoutExpired(access.FIREWALL, 10)
        replies = self.run_preparation(('verify',), [['firewall', self.tokens[0]]], timeout)
        self.assertEqual(replies[0][:2], ['firewall-error', self.tokens[0]])
        self.assertIn('timed out', replies[0][2])
        def unsafe(_):
            raise access.subprocess.CalledProcessError(1, access.FIREWALL, stderr=b'bad\x1b[2J' + b'x' * 3000)
        replies = self.run_preparation(('verify',), [['firewall', self.tokens[0]]], unsafe)
        self.assertNotIn('\x1b', replies[0][2])
        self.assertEqual(len(replies[0][2]), 2000)

    def test_expiration_and_disconnection_do_no_firewall_work(self):
        with patch.object(access.time, 'monotonic', side_effect=[100, 401]), self.assertRaises(PermissionError):
            self.run_preparation((), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.run_preparation((), []), [])
        self.assertEqual(self.calls, [])

    def test_expiration_while_receiving_request_does_no_firewall_work(self):
        with patch.object(access.time, 'monotonic', side_effect=[100, 101, 401]), self.assertRaises(PermissionError):
            self.run_preparation((), [['firewall', self.tokens[0]]])
        self.assertEqual(self.calls, [])

    def test_helper_must_confirm_the_same_policy_and_allowed_action(self):
        for change in [dict(network='mybox-net-other'), dict(ports=[8080])]:
            def changed(index):
                policy = dict(self.policy, **change) if index else self.policy
                return SimpleNamespace(stdout=json.dumps(dict(action='verify' if index else 'apply', policy=policy)).encode())
            with self.subTest(change=change), self.assertRaises(PermissionError):
                self.run_preparation(('apply', 'verify'), [['firewall', t] for t in self.tokens[:2]], changed)
