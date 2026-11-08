"""Exercise the native approval service without root, firewall changes, or UI."""
from pathlib import Path
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from service_protocol import packet, receive

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / '.build/mybox-service-tests'


class BoxAccess(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.policy = dict(owner=499, network='mybox-net-demo', subnet4='192.168.65.0/24',
                           subnet6='fd00::/64', gateway='192.168.65.1', ports=[], active=True)
        self.tokens = [f'{index:08d}-1111-4111-8111-111111111111' for index in range(1, 5)]

    def prompt(self, args):
        response = subprocess.run([NATIVE, 'prompt', json.dumps(args)], capture_output=True, text=True)
        if response.returncode:
            raise ValueError(response.stderr)
        return response.stdout

    def test_prompt_describes_what_the_requester_receives(self):
        self.assertIn('shell', self.prompt(['enter', 'demo']))
        reason = self.prompt(['enter', 'demo', 'cat', 'a b', 'a\nb', '\u202etest'])
        self.assertIn('receives output', reason)
        self.assertIn('"a b" "a\\nb" "\\u202etest"', reason)
        self.assertIn('copy of', self.prompt(['export', 'demo', '/workspace/x']))
        self.assertIn('write', self.prompt(['import', 'demo', 'file']))
        for args in (['enter', 'fake\nname'], ['shell', 'demo'], ['export', 'demo'], ['enter', 'demo', 'a\0b']):
            with self.assertRaises(ValueError):
                self.prompt(args)

    def exchange(self, mode, messages, config):
        log = self.directory / 'calls.json'
        log.unlink(missing_ok=True)
        config = dict(config, log=str(log))
        conn, caller = socket.socketpair()
        with conn, caller:
            process = subprocess.Popen([NATIVE, mode, str(conn.fileno()), json.dumps(config)],
                pass_fds=(conn.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            conn.close()
            caller.settimeout(10)
            for message in messages:
                packet(caller, message)
            caller.shutdown(socket.SHUT_WR)
            answers = []
            try:
                while True:
                    answers.append(receive(caller))
            except (EOFError, ConnectionResetError):
                pass
            _, error = process.communicate(timeout=10)
        self.calls = json.loads(log.read_text())
        return process.returncode, answers, error.decode()

    def run_preparation(self, actions, messages, responses=None, **config):
        if responses is None:
            responses = [dict(confirmation=dict(action=action, policy=self.policy)) for action in actions]
        status, answers, error = self.exchange('prepare', messages, dict(config, responses=responses))
        if status:
            raise ValueError(error)
        for call in self.calls:
            self.assertEqual(call['executable'], '/Library/PrivilegedHelperTools/mybox-firewall-manager')
            self.assertEqual(call['input']['box'], 'demo')
            self.assertLessEqual(call['timeout'], 300)
        return answers

    def test_missing_authentication_replayed_approval_and_fresh_approval(self):
        path = str(self.directory / 'socket')
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(path); listener.listen(); listener.settimeout(10)
            challenges, errors = [], []
            def server():
                try:
                    for index in range(4):
                        conn, _ = listener.accept()
                        with conn:
                            version, challenge, args = receive(conn)
                            self.assertEqual((version, args), ('access-v2', ['enter', 'demo', 'pwd']))
                            challenges.append(challenge)
                            replies = [['denied'], ['approved', challenges[0]], ['approved', challenge], ['approved', True]]
                            packet(conn, replies[index])
                            self.assertEqual(conn.recv(1), b'')
                except Exception as error:
                    errors.append(error)
            thread = threading.Thread(target=server)
            thread.start()
            for allowed in (False, False, True, False):
                result = subprocess.run([NATIVE, 'request', path, str(os.getuid())], capture_output=True, timeout=10)
                self.assertEqual(result.returncode == 0, allowed, result.stderr)
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(set(challenges)), 4)

    def test_service_rejects_canceled_authentication_and_stale_helper_output(self):
        challenge = 'a' * 64
        for status, output, allowed in [(1, challenge, False), (0, 'old', False), (0, challenge, True)]:
            result, answers, error = self.exchange('access', [['access-v2', challenge, ['enter', 'demo', 'pwd']]],
                dict(auth_status=status, auth_output=output))
            self.assertEqual(result == 0, allowed, error)
            self.assertEqual(answers, [['approved', challenge]] if allowed else [])
            self.assertIn('pwd', self.calls[0]['input'][1])

    def test_wrong_peer_and_console_owner_cannot_authenticate(self):
        for config in [dict(peer=os.getuid() + 1), dict(console=502), dict(owner=0)]:
            status, answers, _ = self.exchange('access', [['access-v2', 'a' * 64, ['enter', 'demo']]], config)
            self.assertNotEqual(status, 0)
            self.assertEqual(answers, [])
            self.assertEqual(self.calls, [])

    def test_running_stopped_and_recovered_boxes_finish_without_sudo(self):
        for actions in [('verify',), ('apply', 'verify', 'verify'), ('update-ports', 'verify')]:
            with self.subTest(actions=actions):
                requests = [['firewall', token] for token in self.tokens[:len(actions)]]
                self.assertEqual(self.run_preparation(actions, requests + [['finish']]), requests + [['finished']])
                for index, call in enumerate(self.calls):
                    self.assertEqual(call['input']['policy'], self.policy if index else None)
                    self.assertIn(actions[index], call['input']['actions'])

    def test_cannot_repeat_requests_change_operations_or_finish_before_checks(self):
        for actions, messages in [
            ((), [['finish']]), ((), [['firewall', '../request']]), ((), [['enter', 'other-box']]),
            (('apply',), [['firewall', self.tokens[0]], ['firewall', self.tokens[0]]]),
            (('apply',), [['firewall', self.tokens[0]], ['finish']]),
            (('verify',), [['firewall', self.tokens[0]], ['firewall', self.tokens[1]]]),
        ]:
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                self.run_preparation(actions, messages)

    def test_firewall_failure_cannot_confirm_access(self):
        replies = self.run_preparation((), [['firewall', self.tokens[0]]],
            [dict(status=1, error='The network bridge is missing.')])
        self.assertEqual(replies, [['firewall-error', self.tokens[0], 'The network bridge is missing.']])
        self.assertEqual(len(self.calls), 1)

    def test_firewall_timeout_and_terminal_controls_are_reported_without_approval(self):
        replies = self.run_preparation((), [['firewall', self.tokens[0]]], [dict(timeout=True)])
        self.assertEqual(replies[0][:2], ['firewall-error', self.tokens[0]])
        self.assertIn('timed out', replies[0][2])
        replies = self.run_preparation((), [['firewall', self.tokens[0]]], [dict(status=1, error='bad\x1b[2J' + 'x' * 3000)])
        self.assertNotIn('\x1b', replies[0][2])
        self.assertEqual(len(replies[0][2]), 2000)

    def test_expiration_and_disconnection_do_no_firewall_work(self):
        with self.assertRaises(ValueError):
            self.run_preparation((), [], times=[100, 401])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.run_preparation((), []), [])
        self.assertEqual(self.calls, [])

    def test_expiration_while_receiving_request_does_no_firewall_work(self):
        with self.assertRaises(ValueError):
            self.run_preparation((), [['firewall', self.tokens[0]]], times=[100, 101, 401])
        self.assertEqual(self.calls, [])

    def test_helper_must_confirm_the_same_policy_and_allowed_action(self):
        for change in [dict(network='mybox-net-other'), dict(ports=[8080])]:
            responses = [dict(confirmation=dict(action='apply', policy=self.policy)),
                         dict(confirmation=dict(action='verify', policy=dict(self.policy, **change)))]
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.run_preparation((), [['firewall', t] for t in self.tokens[:2]], responses)
        with self.assertRaises(ValueError):
            self.run_preparation((), [['firewall', self.tokens[0]]], [dict(confirmation=dict(action='remove', policy=self.policy))])
