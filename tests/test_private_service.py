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
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'service'))
import broker
import install as service_installer
import transfer


class PrivateService(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)

    def test_reinstall_accepts_its_own_ids_and_rejects_an_unrelated_owner(self):
        for user, group, allowed in [('_mybox', '_mybox', True), ('someone', '_mybox', False),
                                     ('_mybox', 'someone', False)]:
            with self.subTest(user=user, group=group), \
                 patch.object(service_installer.pwd, 'getpwall', return_value=[SimpleNamespace(pw_name=user, pw_uid=499)]), \
                 patch.object(service_installer.grp, 'getgrall', return_value=[SimpleNamespace(gr_name=group, gr_gid=499)]):
                if allowed:
                    service_installer.check_service_id(499)
                else:
                    with self.assertRaises(ValueError):
                        service_installer.check_service_id(499)

    def test_existing_service_records_are_read_only_during_update(self):
        identity = dict(number=499, user='a-user-id', group='a-group-id')
        record = {'UniqueID': '499', 'PrimaryGroupID': '499', 'NFSHomeDirectory': str(broker.HOME),
                  'UserShell': '/usr/bin/false', 'IsHidden': '1'}

        def directory_read(*args, **kwargs):
            if args[0] == '/usr/bin/id':
                return SimpleNamespace(stdout='_mybox everyone')
            self.assertEqual(args[:3], (service_installer.DSCL, '.', '-read'))
            path, attribute = args[3:]
            if attribute == 'GeneratedUID':
                value = identity['group' if path.startswith('/Groups/') else 'user']
            else:
                value = record[attribute]
            field = 'dsAttrTypeNative:IsHidden' if attribute == 'IsHidden' else attribute
            return SimpleNamespace(returncode=0, stdout=f'{field}: {value}\n', stderr='')

        user = SimpleNamespace(pw_uid=499, pw_gid=499, pw_dir=str(broker.HOME))
        with patch.object(service_installer, 'command', side_effect=directory_read), \
             patch.object(service_installer, 'check_service_id'), \
             patch.object(service_installer.pwd, 'getpwnam', return_value=user):
            self.assertEqual(service_installer.configure_identity(identity), 499)
            record['UserShell'] = '/bin/sh'
            with self.assertRaisesRegex(ValueError, 'unexpected UserShell'):
                service_installer.configure_identity(identity)

    def test_transfer_tree_preserves_bytes_modes_and_skips_links(self):
        source = self.work / 'source'
        source.mkdir()
        (source / '.hidden').write_bytes(b'hello\0world' * 90000)
        (source / '.hidden').chmod(0o641)
        (source / 'empty').mkdir()
        (source / 'link').symlink_to('/etc')
        target = self.work / 'dest'
        target.mkdir()
        stream = io.BytesIO()
        self.assertEqual(transfer.pack(source, stream, skip_special=True), 1)
        stream.seek(0)
        payload = transfer.unpack(stream, target)
        self.assertEqual((payload / '.hidden').read_bytes(), (source / '.hidden').read_bytes())
        self.assertEqual((payload / '.hidden').stat().st_mode & 0o777, 0o641)
        self.assertEqual(sorted(os.listdir(payload)), ['.hidden', 'empty'])
        with self.assertRaises(ValueError):
            transfer.pack(source, io.BytesIO())

    def test_transfer_rejects_forged_entries_and_incomplete_payloads(self):
        for records in [
            [['directory', 'payload'], ['file', '../escape', 0, 0o600]],
            [['directory', 'payload'], ['file', '/etc/passwd', 0, 0o600]],
            [['link', 'payload', '/', 'x']], [['file', 'payload', 20, 0o600]],
            [['file', 'payload', 0, 0o4755]], [['directory', 'payload'], ['directory', '..']],
            [['file', 'payload', 0, 0o600], ['file', 'extra', 0, 0o600]],
        ]:
            with self.subTest(records=records), tempfile.TemporaryDirectory(dir=self.work) as directory:
                stream = io.BytesIO()
                for record in records:
                    transfer.header(stream, record)
                stream.seek(0)
                with self.assertRaises(ValueError):
                    transfer.unpack(stream, directory)
        self.assertFalse((self.work / 'escape').exists())

    def test_export_publishes_once(self):
        source, target = self.work / 'staged', self.work / 'target'
        source.write_text('new')
        target.write_text('existing')
        with self.assertRaises(FileExistsError):
            broker.publish(source, target)
        self.assertEqual(target.read_text(), 'existing')
        target.unlink()
        broker.publish(source, target)
        self.assertEqual(target.read_text(), 'new')

    def test_peer_uid_and_command_allowlist(self):
        with broker.socket_pair() as (server, client):
            self.assertEqual(broker.peer_uid(server), os.getuid())
            with self.assertRaises(PermissionError):
                broker.serve_one(server, os.getuid() + 1, '/usr/bin/false')
        for args in [['setup-network'], ['approve'], ['_test-auth'], ['import', 'demo', '../escape'],
                     ['export', 'demo', '.', '/etc/file'], ['enter', 'a\0b']]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                broker.validate_command(args)

    def session(self, args, body, upload=None):
        script = self.work / 'worker'
        script.write_text('#!/usr/bin/python3\nimport os, pathlib, sys\n' + body)
        script.chmod(0o700)
        with contextlib.ExitStack() as stack:
            server, client = stack.enter_context(broker.socket_pair())
            worker = subprocess.Popen(['/usr/bin/python3', '-I', '-B', str(ROOT / 'service/broker.py'),
                                       'session', str(server.fileno()), str(os.getuid()), str(script)],
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
                    broker.packet(client, ['firewall', False])
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

    def test_worker_aborts_if_firewall_denied(self):
        status, _, _, _ = self.session(['start', 'demo'], """
fd = int(os.environ['MYBOX_FIREWALL_CHANNEL'])
os.write(fd, b'11111111-1111-4111-8111-111111111111')
sys.exit(1 if os.read(fd, 1) == b'N' else 0)
""")
        self.assertEqual(status, 1)

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

    def test_public_cli_and_worker_refuse_direct_backend_access_before_setup(self):
        if (broker.INSTALL / 'broker.py').exists():
            self.skipTest('The private service is already installed.')
        for command in [('mybox', 'list'), ('mybox', 'create'), ('mybox', '_complete-boxes'), ('mybox-worker', 'list')]:
            with self.subTest(command=command):
                result = subprocess.run([ROOT / '.build' / command[0], command[1]], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr.decode(), r'private (account|MyBox|worker)')
