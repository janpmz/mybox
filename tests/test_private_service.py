"""Exercise the real broker protocol with a disposable, same-user worker."""
import array
import contextlib
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
import service_protocol as broker
import transfer_fixture as transfer
import json

NATIVE = ROOT / '.build/mybox-service-tests'
SERVICE = ROOT / '.build/mybox-service'

def native(*args, **kwargs):
    return subprocess.run([NATIVE, *args], capture_output=True, timeout=15, **kwargs)


class PrivateService(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)

    def test_reinstall_accepts_its_own_ids_and_rejects_an_unrelated_owner(self):
        for user, group, allowed in [('_mybox', '_mybox', True), ('someone', '_mybox', False),
                                     ('_mybox', 'someone', False)]:
            with self.subTest(user=user, group=group):
                result = native('check-id', '499', json.dumps([[user, 499], [group, 499]]))
                self.assertEqual(result.returncode == 0, allowed, result.stderr)

    def test_privileged_and_private_helpers_refuse_the_normal_account(self):
        self.assertNotEqual(os.geteuid(), 0, 'Run the development tests as a normal account.')
        for program, args in [('mybox-setup', []), ('mybox-access', ['serve']), ('mybox-service', ['serve'])]:
            with self.subTest(program=program):
                response = subprocess.run([ROOT / '.build' / program, *args], capture_output=True, timeout=10)
                self.assertNotEqual(response.returncode, 0)
                self.assertTrue(response.stderr)

    def test_runtime_migration_only_stops_the_private_account_when_boxes_are_stopped(self):
        for mode, allowed in [('absent', True), ('stopped', True), ('var-alias', True), ('running', False), ('wrong-root', False), ('wrong-install', False)]:
            with self.subTest(mode=mode):
                response = native('legacy-runtime', mode)
                self.assertEqual(response.returncode == 0, allowed, response.stderr)
                if mode == 'stopped':
                    self.assertEqual(response.stdout, b'stopped private runtime\n')

    def test_existing_service_records_are_read_only_during_update(self):
        record = {'UniqueID': '499', 'PrimaryGroupID': '499', 'NFSHomeDirectory': str(broker.HOME),
                  'UserShell': '/usr/bin/false', 'IsHidden': '1'}
        log = self.work / 'calls.json'
        config = dict(record=record, log=str(log))
        result = native('identity', json.dumps(config))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(log.read_text())
        self.assertTrue(all(call[2] == '-read' for call in calls if call[0].endswith('/dscl')))
        self.assertFalse(any('Password' in call for call in calls))
        for changes in [dict(uuid='unrelated'), dict(groups='_mybox admin'),
                        dict(record=dict(record, UserShell='/bin/sh'))]:
            result = native('identity', json.dumps(dict(config, **changes)))
            self.assertNotEqual(result.returncode, 0)

    def test_transfer_tree_preserves_bytes_modes_and_skips_links(self):
        source = self.work / 'source'
        source.mkdir()
        (source / '.hidden').write_bytes(b'hello\0world' * 90000)
        (source / '.hidden').chmod(0o641)
        (source / 'empty').mkdir()
        (source / 'link').symlink_to('/etc')
        target = self.work / 'dest'
        target.mkdir()
        packed = native('pack', str(source), 'skip')
        self.assertEqual((packed.returncode, packed.stderr), (0, b'1\n'))
        unpacked = native('unpack', str(target), input=packed.stdout)
        self.assertEqual(unpacked.returncode, 0, unpacked.stderr)
        payload = Path(unpacked.stdout.decode().strip())
        # Decode native output using the independent previous format too.
        legacy = self.work / 'legacy'; legacy.mkdir()
        self.assertEqual((transfer.unpack(io.BytesIO(packed.stdout), legacy) / '.hidden').read_bytes(), (source / '.hidden').read_bytes())
        self.assertEqual((payload / '.hidden').read_bytes(), (source / '.hidden').read_bytes())
        self.assertEqual((payload / '.hidden').stat().st_mode & 0o777, 0o641)
        self.assertEqual(sorted(os.listdir(payload)), ['.hidden', 'empty'])
        self.assertNotEqual(native('pack', str(source)).returncode, 0)

    def test_transfer_rejects_forged_entries_and_incomplete_payloads(self):
        for records in [
            [['directory', 'payload'], ['file', '../escape', 0, 0o600]],
            [['directory', 'payload'], ['file', '/etc/passwd', 0, 0o600]],
            [['link', 'payload', '/', 'x']], [['file', 'payload', 20, 0o600]],
            [['file', 'payload', 0, 0o4755]], [['directory', 'payload'], ['directory', '..']],
            [['file', 'payload', 0, 0o600], ['file', 'extra', 0, 0o600]],
            [['file', 'payload', True, 0o600]], [['file', 'payload', 0, False]],
            [['file', 'payload', -1, 0o600]], [['file', 'payload', 0.5, 0o600]],
            [['directory', 'payload'], ['file', 'duplicate', 0, 0o600], ['file', 'duplicate', 0, 0o600]],
        ]:
            with self.subTest(records=records), tempfile.TemporaryDirectory(dir=self.work) as directory:
                stream = io.BytesIO()
                for record in records:
                    transfer.header(stream, record)
                stream.seek(0)
                self.assertNotEqual(native('unpack', directory, input=stream.read()).returncode, 0)
        self.assertFalse((self.work / 'escape').exists())

    def test_export_publishes_once(self):
        source, target = self.work / 'staged', self.work / 'target'
        source.write_text('new')
        target.write_text('existing')
        self.assertNotEqual(native('publish', str(source), str(target)).returncode, 0)
        self.assertEqual(target.read_text(), 'existing')
        target.unlink()
        self.assertEqual(native('publish', str(source), str(target)).returncode, 0)
        self.assertEqual(target.read_text(), 'new')

    def test_peer_uid_and_command_allowlist(self):
        with broker.socket_pair() as (server, client):
            result = subprocess.run([NATIVE, 'peer', str(server.fileno()), str(os.getuid())],
                                    pass_fds=(server.fileno(),), capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            process = subprocess.Popen([SERVICE, 'session', str(server.fileno()), str(os.getuid() + 1), '/usr/bin/false'],
                                       pass_fds=(server.fileno(),))
            server.close()
            self.assertEqual(broker.receive(client)[0], 'error')
            process.wait(timeout=10)
        for args in [['setup-network'], ['approve'], ['_test-auth'], ['import', 'demo', '../escape'],
                     ['export', 'demo', '.', '/etc/file'], ['enter', 'a\0b']]:
            with self.subTest(args=args):
                self.assertNotEqual(native('validate', json.dumps(args)).returncode, 0)

    def session(self, args, body, upload=None, reply=None):
        script = self.work / 'worker'
        script.write_text('#!/usr/bin/python3\nimport os, pathlib, sys\n' + body)
        script.chmod(0o700)
        with contextlib.ExitStack() as stack:
            server, client = stack.enter_context(broker.socket_pair())
            worker = subprocess.Popen([SERVICE, 'session', str(server.fileno()), str(os.getuid()), str(script)],
                                      pass_fds=(server.fileno(),))
            server.close()
            src, out, err, spool = [stack.enter_context(tempfile.TemporaryFile()) for _ in range(4)]
            if upload:
                transfer.pack(upload, spool)
                spool.seek(0)
            src.write(b'piped input')
            src.seek(0)
            broker.packet(client, args)
            client.sendmsg([b'F'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                     array.array('i', [s.fileno() for s in (src, out, err, spool)]))])
            client.settimeout(10)
            try:
                message = broker.receive(client)
                if message[0] == 'firewall':
                    self.assertEqual(message[1], '11111111-1111-4111-8111-111111111111')
                    broker.packet(client, reply or ['firewall', False])
                    message = broker.receive(client)
                self.assertEqual(message[0], 'exit', message)
            finally:
                client.close()
                worker.wait(timeout=10)
            for stream in (out, err, spool):
                stream.seek(0)
            return message[1], out.read(), err.read(), spool.read()

    def test_worker_forwards_arguments_stdio_and_exit_status(self):
        status, out, err, _ = self.session(['enter', 'demo', '--', 'one argument'],
            "print(sys.argv[-1], sys.stdin.read()); print('worker error', file=sys.stderr); sys.exit(37)")
        self.assertEqual((status, out, err), (37, b'one argument piped input\n', b'worker error\n'))

    def test_delete_command_reaches_the_worker_with_confirmation_flag(self):
        status, out, err, _ = self.session(['delete', 'demo', '--yes'], 'print(repr(sys.argv[1:]))')
        self.assertEqual((status, out, err), (0, b"['delete', 'demo', '--yes']\n", b''))

    def test_json_list_reaches_worker_without_access_approval(self):
        status, out, err, _ = self.session(['list', '--json'],
            "assert sys.argv[1:] == ['list', '--json']; print('{\"schemaVersion\":1,\"boxes\":[]}')")
        self.assertEqual((status, out, err), (0, b'{"schemaVersion":1,"boxes":[]}\n', b''))

    def test_worker_aborts_if_firewall_denied(self):
        status, _, _, _ = self.session(['start', 'demo'], """
fd = int(os.environ['MYBOX_FIREWALL_CHANNEL'])
os.write(fd, b'11111111-1111-4111-8111-111111111111')
sys.exit(1 if os.read(fd, 1) == b'N' else 0)
""")
        self.assertEqual(status, 1)

    def test_worker_inherits_only_stdio_and_firewall_channel(self):
        status, out, err, _ = self.session(['list'], """
channel = int(os.environ['MYBOX_FIREWALL_CHANNEL'])
for fd in range(3, 128):
    try:
        os.fstat(fd)
    except OSError:
        continue
    assert fd == channel, (fd, channel)
print('checked')
""")
        self.assertEqual((status, out, err), (0, b'checked\n', b''))

    def test_interrupt_reaches_worker_process_group(self):
        status, out, err, _ = self.session(['enter', 'demo'], """
import signal
signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
os.write(int(os.environ['MYBOX_FIREWALL_CHANNEL']), b'11111111-1111-4111-8111-111111111111')
signal.pause()
""", reply=['signal', 'INT'])
        self.assertEqual((status, out, err), (130, b'', b''))

    def test_truncated_descriptor_message_is_rejected(self):
        with broker.socket_pair() as (server, client), tempfile.TemporaryFile() as stream:
            process = subprocess.Popen([SERVICE, 'session', str(server.fileno()), str(os.getuid()), '/usr/bin/false'],
                pass_fds=(server.fileno(),))
            server.close()
            broker.packet(client, ['list'])
            client.sendmsg([b'F'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [stream.fileno()] * 5))])
            client.settimeout(10)
            self.assertEqual(broker.receive(client), ['error', 'Invalid command streams.'])
            process.wait(timeout=10)

    def test_disconnect_terminates_running_worker(self):
        script = self.work / 'worker'
        pidfile = self.work / 'pid'
        script.write_text('#!/usr/bin/python3\nimport os, pathlib, time\n'
            + f'pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\n'
            + "os.write(int(os.environ['MYBOX_FIREWALL_CHANNEL']), b'11111111-1111-4111-8111-111111111111')\ntime.sleep(60)\n")
        script.chmod(0o700)
        with broker.socket_pair() as (server, client), contextlib.ExitStack() as stack:
            process = subprocess.Popen([SERVICE, 'session', str(server.fileno()), str(os.getuid()), str(script)],
                pass_fds=(server.fileno(),))
            server.close()
            streams = [stack.enter_context(tempfile.TemporaryFile()) for _ in range(4)]
            broker.packet(client, ['enter', 'demo'])
            client.sendmsg([b'F'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [s.fileno() for s in streams]))])
            client.settimeout(10)
            self.assertEqual(broker.receive(client)[0], 'firewall')
            client.close()
            process.wait(timeout=10)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)

    def test_worker_receives_only_private_transfer_paths(self):
        source = self.work / 'original'
        source.write_bytes(b'project')
        status, out, _, _ = self.session(['import', 'demo', 'files'],
            "p = pathlib.Path(sys.argv[3]); print(p.name, p.read_text()); assert p.parent.name == 'input'", source)
        self.assertEqual((status, out), (0, b'files project\n'))
        status, _, _, data = self.session(['export', 'demo', '/workspace/file'],
            "p = pathlib.Path(sys.argv[4]); assert p.name == 'payload'; p.write_text('result')")
        self.assertEqual(status, 0)
        self.assertEqual(transfer.unpack(io.BytesIO(data), self.work).read_text(), 'result')

    def native_client(self, args, body, expected_peer=None):
        script = self.work / 'client-worker'
        script.write_text('#!/usr/bin/python3\nimport os, pathlib, sys\n' + body)
        script.chmod(0o700)
        path = str(self.work / 'client.sock')
        Path(path).unlink(missing_ok=True)
        errors = []
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(path); listener.listen(); listener.settimeout(10)
            def server():
                try:
                    conn, _ = listener.accept()
                    with conn:
                        process = subprocess.run([SERVICE, 'session', str(conn.fileno()), str(os.getuid()), str(script)],
                            pass_fds=(conn.fileno(),), capture_output=True, timeout=15)
                        self.assertEqual(process.returncode, 0, process.stderr)
                except Exception as error:
                    errors.append(error)
            thread = threading.Thread(target=server)
            thread.start()
            response = native('client', path, str(os.getuid() if expected_peer is None else expected_peer), *args, input=b'input')
            thread.join(20)
            self.assertFalse(thread.is_alive())
            if errors:
                raise errors[0]
            return response

    def test_native_client_and_service_preserve_stdio_and_exit(self):
        response = self.native_client(['enter', 'demo', 'one argument'],
            "print(sys.argv[-1], sys.stdin.read()); print('stderr', file=sys.stderr); sys.exit(37)")
        self.assertEqual((response.returncode, response.stdout, response.stderr), (37, b'one argument input\n', b'stderr\n'))

    def test_native_client_and_service_transfer_files(self):
        source = self.work / 'project.txt'; source.write_text('project')
        response = self.native_client(['import', 'demo', str(source)],
            "p = pathlib.Path(sys.argv[3]); assert p.name == 'project.txt'; assert p.read_text() == 'project'")
        self.assertEqual(response.returncode, 0, response.stderr)
        target = self.work / 'exported.txt'
        response = self.native_client(['export', 'demo', '/workspace/file', str(target)],
            "pathlib.Path(sys.argv[4]).write_text('exported')")
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(target.read_text(), 'exported')
        self.assertIn(b'Exported to', response.stdout)
        response = self.native_client(['export', 'demo', '/workspace/file', str(target)], "raise RuntimeError('must not run')")
        self.assertNotEqual(response.returncode, 0)
        self.assertEqual(target.read_text(), 'exported')

    def test_native_client_rejects_wrong_service_owner(self):
        response = self.native_client(['list'], "raise RuntimeError('must not run')", expected_peer=os.getuid() + 1)
        self.assertNotEqual(response.returncode, 0)
        self.assertIn(b'did not reach the private MyBox account', response.stderr)

    def test_public_cli_and_worker_refuse_direct_backend_access_before_setup(self):
        if (broker.INSTALL / 'mybox-service').exists() or (broker.INSTALL / 'broker.py').exists():
            self.skipTest('The private service is already installed.')
        for command in [('mybox', 'list'), ('mybox', 'create'), ('mybox', '_complete-boxes'), ('mybox-worker', 'list')]:
            with self.subTest(command=command):
                result = subprocess.run([ROOT / '.build' / command[0], command[1]], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr.decode(), r'private (account|MyBox|worker)')
