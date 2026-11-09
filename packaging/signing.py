"""Developer ID signing and optional notarization for the staged MyBox package."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess


def run(*arguments, **options):
    return subprocess.run(arguments, check=True, capture_output=True, text=True, **options)


def identities(policy):
    output = run('/usr/bin/security', 'find-identity', '-v', '-p', policy).stdout
    return [dict(sha1=digest, name=name) for digest, name in
            re.findall(r'\d+\)\s+([A-Fa-f0-9]{40})\s+"([^"]+)"', output)]


def choose_identity(available, kind, requested):
    candidates = [item for item in available if item['name'].startswith(kind + ': ')]
    if requested:
        candidates = [item for item in candidates if requested in (item['name'], item['sha1'])]
    if len(candidates) != 1:
        raise ValueError(f'Expected one {kind} identity with its private key; found {len(candidates)}. '
                         'Install the certificate in Keychain Access, or select its full name explicitly.')
    selected = candidates[0]
    # productsign selects by common name. Reject ambiguous names rather than let
    # it choose between old and replacement certificates unpredictably.
    if kind == 'Developer ID Installer' and sum(item['name'] == selected['name'] for item in available) != 1:
        raise ValueError('Multiple Installer identities share this name. Keep only the intended identity '
                         'in the keychain search list before signing.')
    match = re.search(r'\(([A-Z0-9]{10})\)$', selected['name'])
    if not match:
        raise ValueError(f'Cannot determine the Apple team for {selected["name"]}')
    return dict(selected, team_id=match[1])


def configure(output, notarize=False):
    application = choose_identity(identities('codesigning'), 'Developer ID Application',
                                  os.environ.get('MYBOX_APPLICATION_IDENTITY'))
    installer = choose_identity(identities('basic'), 'Developer ID Installer',
                                os.environ.get('MYBOX_INSTALLER_IDENTITY'))
    if application['team_id'] != installer['team_id']:
        raise ValueError('Application and Installer certificates must belong to the same Apple team.')
    profile = os.environ.get('MYBOX_NOTARY_PROFILE', '')
    if notarize and not profile:
        raise ValueError('Set MYBOX_NOTARY_PROFILE to the notarytool credentials stored in your keychain.')
    config = dict(mode='developer-id', team_id=application['team_id'], application=application,
                  installer=installer, notarized=False)
    output.write_text(json.dumps(config, indent=2) + '\n')
    print(f'Signing as {application["name"]}')


def machos(root):
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as stream:
            if stream.read(4) in (b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'):
                yield path


def entitlements(path):
    result = run('/usr/bin/codesign', '-d', '--entitlements', ':-', str(path))
    return plistlib.loads(result.stdout.encode()) if result.stdout.strip() else {}


def verify_code(path, team):
    requirement = ('anchor apple generic and '
                   'certificate leaf[field.1.2.840.113635.100.6.1.13] exists and '
                   f'certificate leaf[subject.OU] = "{team}"')
    run('/usr/bin/codesign', '--verify', '--strict', '-R', '=' + requirement, str(path))
    details = run('/usr/bin/codesign', '-d', '--verbose=4', str(path)).stderr
    flags = re.search(r'flags=0x([0-9a-fA-F]+)', details)
    if not flags or not int(flags[1], 16) & 0x10000:
        raise ValueError(f'Hardened runtime is missing: {path}')
    if not re.search(r'^Timestamp=.+$', details, re.MULTILINE):
        raise ValueError(f'Secure signing timestamp is missing: {path}')
    if entitlements(path).get('com.apple.security.get-task-allow'):
        raise ValueError(f'Development debugger entitlement is present: {path}')


def sign_code(path, config):
    original = entitlements(path)
    if original.get('com.apple.security.get-task-allow'):
        raise ValueError(f'Rebuild without the development debugger entitlement: {path}')
    run('/usr/bin/codesign', '--force', '--sign', config['application']['sha1'],
        '--options', 'runtime', '--timestamp', '--preserve-metadata=identifier,entitlements', str(path))
    if entitlements(path) != original:
        raise ValueError(f'Signing changed required entitlements: {path}')
    verify_code(path, config['team_id'])


def sign_payload(payload, scripts, config):
    binaries = list(machos(payload))
    if not binaries:
        raise ValueError('No executables found in the package payload.')
    for binary in binaries:
        print(f'Signing {binary.relative_to(payload)}', flush=True)
        sign_code(binary, config)
    # Seal the app after its executable. Do not use --deep for signing.
    sign_code(payload / 'Applications/MyBox.app', config)
    setup = payload / 'Library/PrivilegedHelperTools/io.github.janpmz.my-container-box.service/mybox-setup'
    # Use identical signed bytes in the installer scripts and installed payload.
    shutil.copyfile(setup, scripts / 'mybox-setup')
    verify_code(scripts / 'mybox-setup', config['team_id'])


def verify_package(package, expected_team=None):
    result = subprocess.run(['/usr/sbin/pkgutil', '--check-signature', str(package)],
                            capture_output=True, text=True)
    text = result.stdout + result.stderr
    if expected_team is None and 'status: no signature' in text.lower():
        return dict(mode='ad-hoc', notarized=False)
    if result.returncode != 0:
        raise ValueError(f'Installer signature verification failed:\n{text}')
    match = re.search(r'Developer ID Installer: (.+) \(([A-Z0-9]{10})\)', text)
    status = re.search(r'^\s*Status:\s*(.+)$', text, re.MULTILINE)
    trusted_statuses = {
        'signed by a certificate trusted by Mac OS X',
        'signed by a certificate trusted by macOS',
        'signed by a developer certificate issued by Apple for distribution',
    }
    if not match or not status or status[1].strip() not in trusted_statuses:
        raise ValueError(f'The package does not have a trusted Developer ID Installer signature:\n{text}')
    if 'signed with a trusted timestamp' not in text.lower():
        raise ValueError('The installer signature has no trusted timestamp.')
    team = match[2]
    if expected_team and team != expected_team:
        raise ValueError(f'Installer team {team} does not match application team {expected_team}.')
    return dict(mode='developer-id', team_id=team)


def sign_package(package, config):
    signed = package.with_name(package.stem + '-signed.pkg')
    run('/usr/bin/productsign', '--sign', config['installer']['name'], '--timestamp', str(package), str(signed))
    verify_package(signed, config['team_id'])
    signed.replace(package)


def notarize_package(package, config, config_path):
    profile = os.environ.get('MYBOX_NOTARY_PROFILE')
    if not profile:
        raise ValueError('MYBOX_NOTARY_PROFILE is required for notarization.')
    submission = subprocess.run(['/usr/bin/xcrun', 'notarytool', 'submit', str(package),
                                 '--keychain-profile', profile, '--wait', '--timeout', '30m',
                                 '--output-format', 'json'], capture_output=True, text=True)
    (package.parent / 'notarization-submission.json').write_text(submission.stdout)
    try:
        response = json.loads(submission.stdout)
    except json.JSONDecodeError:
        raise ValueError(f'Notarization did not return a submission status: {submission.stderr}') from None
    if response.get('id'):
        run('/usr/bin/xcrun', 'notarytool', 'log', response['id'], '--keychain-profile', profile,
            str(package.parent / 'notarization-log.json'))
    if submission.returncode != 0 or response.get('status') != 'Accepted':
        raise ValueError(f'Apple did not accept the package: {response.get("status")}. See the notarization log.')
    run('/usr/bin/xcrun', 'stapler', 'staple', str(package))
    run('/usr/bin/xcrun', 'stapler', 'validate', str(package))
    run('/usr/sbin/spctl', '--assess', '--type', 'install', '--verbose=2', str(package))
    config.update(notarized=True, notarization_id=response['id'])
    config_path.write_text(json.dumps(config, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['configure', 'payload', 'package'])
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--payload', type=Path)
    parser.add_argument('--scripts', type=Path)
    parser.add_argument('--package', type=Path)
    parser.add_argument('--notarize', action='store_true')
    options = parser.parse_args()
    if options.action == 'configure':
        configure(options.config, options.notarize)
        return
    config = json.loads(options.config.read_text())
    if options.action == 'payload':
        if not options.payload or not options.scripts:
            parser.error('payload requires --payload and --scripts')
        sign_payload(options.payload, options.scripts, config)
    else:
        if not options.package:
            parser.error('package requires --package')
        sign_package(options.package, config)
        if options.notarize:
            notarize_package(options.package, config, options.config)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        raise SystemExit(f'Signing failed: {detail or error}')
