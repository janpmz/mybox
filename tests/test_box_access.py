"""Check the private service's one-use authentication protocol without displaying UI."""
from pathlib import Path
import os
import socket
import sys
import tempfile
import threading
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
                        challenge, args = receive(conn)
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
                    packet(caller, [challenge, ['enter', 'demo', 'pwd']])
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
