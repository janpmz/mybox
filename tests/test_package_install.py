"""Exercise package ownership and upgrade decisions without administrator access."""
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT/'.build/mybox-service-tests'


class PackageInstall(unittest.TestCase):
    def native(self, *args):
        return subprocess.run([NATIVE, *args], capture_output=True, text=True, timeout=10)

    def test_shared_helper_directory_accepts_sticky_bit_without_chmod(self):
        with tempfile.TemporaryDirectory() as directory:
            for mode in [0o755, 0o1755]:
                os.chmod(directory, mode)
                response = self.native('install-directory', directory, str(os.getuid()), str(os.getgid()), 'sticky')
                self.assertEqual(response.returncode, 0, response.stderr)
                self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), mode)

    def test_shared_helper_directory_still_rejects_unsafe_modes_and_owners(self):
        with tempfile.TemporaryDirectory() as directory:
            for mode in [0o777, 0o1777, 0o775, 0o2755, 0o4755, 0o700]:
                os.chmod(directory, mode)
                response = self.native('install-directory', directory, str(os.getuid()), str(os.getgid()), 'sticky')
                self.assertNotEqual(response.returncode, 0, oct(mode))
            os.chmod(directory, 0o755)
            response = self.native('install-directory', directory, str(os.getuid() + 1), str(os.getgid()), 'sticky')
            self.assertNotEqual(response.returncode, 0)
            response = self.native('install-directory', directory, str(os.getuid()), str(os.getgid() + 1), 'sticky')
            self.assertNotEqual(response.returncode, 0)
            target = Path(directory)/'link'
            target.symlink_to(directory, target_is_directory=True)
            response = self.native('install-directory', str(target), str(os.getuid()), str(os.getgid()), 'sticky')
            self.assertNotEqual(response.returncode, 0)

    def test_mybox_owned_directories_still_require_exact_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o1755)
            response = self.native('install-directory', directory, str(os.getuid()), str(os.getgid()), 'exact')
            self.assertNotEqual(response.returncode, 0)

    def test_existing_system_helper_directory_passes_without_changes(self):
        directory = Path('/Library/PrivilegedHelperTools')
        before = directory.stat()
        response = self.native('install-directory', str(directory), '0', '0', 'sticky')
        self.assertEqual(response.returncode, 0, response.stderr)
        after = directory.stat()
        self.assertEqual((before.st_mode, before.st_uid, before.st_gid, before.st_mtime_ns),
                         (after.st_mode, after.st_uid, after.st_gid, after.st_mtime_ns))

    def test_first_install_requires_a_signed_in_regular_account(self):
        for uid in [0, 499, 65534]:
            self.assertNotEqual(self.native('package-owner', json.dumps(dict(console=uid))).returncode, 0)
        self.assertNotEqual(self.native('package-owner', '{}').returncode, 0)
        self.assertEqual(self.native('package-owner', '{"console":501}').stdout, '501\n')

    def test_only_startup_disk_is_accepted_as_package_target(self):
        for target in ['/', '/.', '///']:
            with self.subTest(target=target):
                response = self.native('package-target', target)
                self.assertEqual(response.returncode, 0, response.stderr)
        for target in ['', '.', '../../../../..', 'CurrentUserHomeDirectory',
                       '/Users/example', '/Users/example/Applications',
                       '/Volumes/External', '/System/Volumes/Data']:
            with self.subTest(target=target):
                response = self.native('package-target', target)
                self.assertNotEqual(response.returncode, 0)
                self.assertIn('current startup disk', response.stderr)

    def test_upgrade_preserves_owner_and_rejects_another_console_account(self):
        for console in [501, None]:
            values = dict(existing=501)
            if console is not None:
                values['console'] = console
            self.assertEqual(self.native('package-owner', json.dumps(values)).stdout, '501\n')
        self.assertNotEqual(self.native('package-owner', '{"existing":501,"console":502}').returncode, 0)

    def test_personal_fork_must_stop_in_every_account_and_domain(self):
        self.assertEqual(self.native('package-personal', 'none').returncode, 0)
        for domain in ['gui/501', 'user/501', 'gui/502', 'user/502']:
            response = self.native('package-personal', domain+'/io.github.janpmz.mybox.runtime.apiserver')
            self.assertNotEqual(response.returncode, 0)
            self.assertIn('my-container system stop', response.stderr)

    def test_upgrade_checks_private_runtime_without_mutating_it(self):
        for mode, allowed in [('absent', True), ('stopped', True), ('running', False),
                              ('unknown', False), ('wrong-root', False), ('wrong-install', False),
                              ('broken', False), ('legacy', False)]:
            with self.subTest(mode=mode):
                response = self.native('package-runtime', mode)
                self.assertEqual(response.returncode == 0, allowed, response.stderr)

    def test_package_entry_points_refuse_unprivileged_execution(self):
        for flag in ['--package-preflight', '--package-install']:
            response = subprocess.run([ROOT/'.build/mybox-setup', flag, '/'], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(response.returncode, 0)
            self.assertIn('administrator authentication', response.stderr)


if __name__ == '__main__':
    unittest.main()
