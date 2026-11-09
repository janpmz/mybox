import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'packaging'))
import signing


class PackageSigningTests(unittest.TestCase):
    def identity(self, kind='Application', team='ABCDEFGHIJ', digest='A' * 40):
        return dict(name=f'Developer ID {kind}: Example ({team})', sha1=digest)

    def test_identity_selection_rejects_wrong_type_and_ambiguity(self):
        app = self.identity()
        installer = self.identity('Installer')
        selected = signing.choose_identity([app, installer], 'Developer ID Application', None)
        self.assertEqual(selected['team_id'], 'ABCDEFGHIJ')
        with self.assertRaises(ValueError):
            signing.choose_identity([installer], 'Developer ID Application', installer['name'])
        duplicate = self.identity('Installer', digest='B' * 40)
        with self.assertRaises(ValueError):
            signing.choose_identity([installer, duplicate], 'Developer ID Installer', installer['sha1'])

    def test_mismatched_teams_do_not_write_signing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'signing.json'
            with patch.object(signing, 'identities', side_effect=[
                    [self.identity()], [self.identity('Installer', team='KLMNOPQRST')]]), \
                    patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, 'same Apple team'):
                    signing.configure(output)
            self.assertFalse(output.exists())

    def test_release_requires_keychain_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'signing.json'
            with patch.object(signing, 'identities', side_effect=[
                    [self.identity()], [self.identity('Installer')]]), patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, 'MYBOX_NOTARY_PROFILE'):
                    signing.configure(output, notarize=True)
            self.assertFalse(output.exists())

    def test_unsigned_package_is_never_accepted_as_developer_id(self):
        unsigned = subprocess.CompletedProcess([], 1, 'Status: no signature\n', '')
        with patch.object(signing.subprocess, 'run', return_value=unsigned):
            self.assertEqual(signing.verify_package(Path('sample.pkg'))['mode'], 'ad-hoc')
            with self.assertRaises(ValueError):
                signing.verify_package(Path('sample.pkg'), expected_team='ABCDEFGHIJ')

    def test_debugger_entitlement_is_rejected_before_signing(self):
        with patch.object(signing, 'entitlements', return_value={'com.apple.security.get-task-allow': True}), \
                patch.object(signing, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'debugger entitlement'):
                signing.sign_code(Path('helper'), {})
            command.assert_not_called()

    def test_installer_accepts_apple_distribution_status_and_checks_team_and_timestamp(self):
        for status in ['signed by a certificate trusted by Mac OS X',
                       'signed by a developer certificate issued by Apple for distribution']:
            output = (f'Status: {status}\nSigned with a trusted timestamp on: 2026-11-08\n'
                      '1. Developer ID Installer: Example (ABCDEFGHIJ)\n')
            response = subprocess.CompletedProcess([], 0, output, '')
            with patch.object(signing.subprocess, 'run', return_value=response):
                self.assertEqual(signing.verify_package(Path('sample.pkg'), 'ABCDEFGHIJ')['mode'], 'developer-id')
                with self.assertRaisesRegex(ValueError, 'does not match'):
                    signing.verify_package(Path('sample.pkg'), 'KLMNOPQRST')
            response.stdout = output.replace('Signed with a trusted timestamp', 'Signed without a timestamp')
            with patch.object(signing.subprocess, 'run', return_value=response):
                with self.assertRaisesRegex(ValueError, 'no trusted timestamp'):
                    signing.verify_package(Path('sample.pkg'), 'ABCDEFGHIJ')

    def test_installer_rejects_untrusted_status_even_with_the_expected_name(self):
        response = subprocess.CompletedProcess([], 0,
            'Status: signed by an untrusted certificate\n'
            'Signed with a trusted timestamp on: 2026-11-08\n'
            '1. Developer ID Installer: Example (ABCDEFGHIJ)\n', '')
        with patch.object(signing.subprocess, 'run', return_value=response):
            with self.assertRaisesRegex(ValueError, 'trusted Developer ID'):
                signing.verify_package(Path('sample.pkg'), 'ABCDEFGHIJ')

    def test_payload_signs_helpers_and_copies_signed_installer_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload, scripts = root/'payload', root/'scripts'
            scripts.mkdir()
            setup = payload/'Library/PrivilegedHelperTools/io.github.janpmz.my-container-box.service/mybox-setup'
            app = payload/'Applications/MyBox.app'
            paths = [setup, app/'Contents/MacOS/MyBox', payload/'usr/local/bin/mybox',
                     payload/'Library/Application Support/MyBox/Runtime/bin/container']
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'\xcf\xfa\xed\xfe' + b'fixture')
            outside = root/'outside'
            outside.write_bytes(b'\xcf\xfa\xed\xfe' + b'outside')
            (payload/'outside-link').symlink_to(outside)
            signed = []

            def sign(path, config):
                signed.append(path)
                if path.is_file():
                    path.write_bytes(path.read_bytes() + b'signed')

            with patch.object(signing, 'sign_code', side_effect=sign), patch.object(signing, 'verify_code'):
                signing.sign_payload(payload, scripts, dict(team_id='ABCDEFGHIJ'))
            self.assertEqual(set(signed), set(paths + [app]))
            self.assertEqual(signed[-1], app)
            self.assertEqual((scripts/'mybox-setup').read_bytes(), setup.read_bytes())
            self.assertEqual(outside.read_bytes(), b'\xcf\xfa\xed\xfeoutside')

    def test_rejected_notarization_keeps_log_and_never_staples(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            response = subprocess.CompletedProcess([], 65, json.dumps(dict(id='submission-id', status='Invalid')), '')
            config = dict(notarized=False)
            with patch.dict(os.environ, {'MYBOX_NOTARY_PROFILE': 'test-profile'}), \
                    patch.object(signing.subprocess, 'run', return_value=response), \
                    patch.object(signing, 'run') as command:
                with self.assertRaisesRegex(ValueError, 'Invalid'):
                    signing.notarize_package(root/'sample.pkg', config, root/'signing.json')
            command.assert_called_once_with('/usr/bin/xcrun', 'notarytool', 'log', 'submission-id',
                                             '--keychain-profile', 'test-profile', str(root/'notarization-log.json'))
            self.assertEqual(json.loads((root/'notarization-submission.json').read_text())['status'], 'Invalid')
            self.assertFalse(config['notarized'])

    def test_gatekeeper_rejection_does_not_mark_release_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            response = subprocess.CompletedProcess([], 0, json.dumps(dict(id='submission-id', status='Accepted')), '')
            config = dict(notarized=False)

            def check(*arguments):
                if arguments[0] == '/usr/sbin/spctl':
                    raise subprocess.CalledProcessError(1, arguments)

            with patch.dict(os.environ, {'MYBOX_NOTARY_PROFILE': 'test-profile'}), \
                    patch.object(signing.subprocess, 'run', return_value=response), \
                    patch.object(signing, 'run', side_effect=check):
                with self.assertRaises(subprocess.CalledProcessError):
                    signing.notarize_package(root/'sample.pkg', config, root/'signing.json')
            self.assertFalse(config['notarized'])
            self.assertFalse((root/'signing.json').exists())


if __name__ == '__main__':
    unittest.main()
