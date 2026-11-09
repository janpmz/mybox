"""Extract and inspect the real package without installing or starting services."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'packaging'))
from signing import verify_code, verify_package


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, timeout=60, **kwargs)


def verify(package):
    checks = []
    signing = verify_package(package)
    if signing['mode'] == 'developer-id':
        staple = subprocess.run(['/usr/bin/xcrun', 'stapler', 'validate', str(package)], capture_output=True)
        signing['notarized'] = staple.returncode == 0
        if signing['notarized']:
            run('/usr/sbin/spctl', '--assess', '--type', 'install', str(package))
        checks.append('Installer has a trusted Developer ID signature and secure timestamp.')
    with tempfile.TemporaryDirectory(prefix='mybox-package-check-') as directory:
        expanded = Path(directory)/'expanded'
        run('/usr/sbin/pkgutil', '--expand-full', str(package), str(expanded))
        distribution = ET.parse(expanded/'Distribution').getroot()
        assert distribution.find('options').get('hostArchitectures') == 'arm64'
        assert distribution.find('options').get('customize') == 'never'
        assert distribution.find('volume-check/allowed-os-versions/os-version').get('min') == '26.0'
        assert distribution.find('options').get('rootVolumeOnly') is None
        assert distribution.find('domains').attrib == dict(enable_localSystem='true',
                                                           enable_currentUserHome='false', enable_anywhere='false')
        assert distribution.find('readme') is None
        welcome = distribution.find('welcome')
        assert welcome is not None and welcome.get('file') == 'Welcome.html'
        welcome_files = list(expanded.rglob('Welcome.html'))
        assert len(welcome_files) == 1, welcome_files
        welcome_text = welcome_files[0].read_text()
        assert all(text in welcome_text for text in ['Touch ID', 'administrator password', 'background services', 'network protection'])
        checks.append('Welcome explains administrator approval; the separate Read Me is omitted.')
        domains = plistlib.loads(run('/usr/sbin/installer', '-pkg', str(package), '-dominfo', '-plist').stdout)
        volumes = plistlib.loads(run('/usr/sbin/installer', '-pkg', str(package), '-volinfo', '-plist').stdout)
        assert domains == [{'Domain': 'LocalSystem'}], domains
        assert volumes == [{'MountPoint': '/'}], volumes
        checks.append('macOS Installer enables only system installation on the startup disk; home-directory and other-volume installations are unavailable.')
        infos = list(expanded.rglob('PackageInfo'))
        assert len(infos) == 1, infos
        component = infos[0].parent
        info = ET.parse(infos[0]).getroot()
        assert info.get('identifier') == 'io.github.janpmz.mybox'
        assert info.get('install-location') == '/'
        assert info.get('version') == '0.1.0'
        assert info.find('scripts/preinstall') is not None and info.find('scripts/postinstall') is not None
        checks.append('One mandatory component installs the app, CLI, runtime, and helpers on macOS 26+ arm64.')
        payload = component/'Payload'
        app = payload/'Applications/MyBox.app'
        helpers = payload/'Library/PrivilegedHelperTools/io.github.janpmz.my-container-box.service'
        runtime = payload/'Library/Application Support/MyBox/Runtime'
        scripts = component/'Scripts'
        assert app.is_dir()
        plist = plistlib.loads((app/'Contents/Info.plist').read_bytes())
        assert plist['LSMinimumSystemVersion'] == '26.0'
        assert plist['CFBundleShortVersionString'] == '0.1.0'
        assert (app/'Contents/Resources/cli-location.txt').read_text().strip() == '/usr/local/bin/mybox'
        assert not (payload/'usr/local/bin/container').exists()
        assert not (runtime/'bin/uninstall-my-container.sh').exists()
        assert not (payload/'private/var/db/mybox-runtime').exists()
        assert not (helpers/'owner').exists() and not (helpers/'identity.plist').exists()
        assert not list(payload.rglob('*.py')) and not list(payload.rglob('*.swift'))
        for name in ['mybox-service', 'mybox-access', 'mybox-worker', 'mybox-authenticate', 'mybox-port-forwarder', 'mybox-setup']:
            assert os.access(helpers/name, os.X_OK), name
        for name in ['preinstall', 'postinstall', 'mybox-setup']:
            assert os.access(scripts/name, os.X_OK), name
        assert (scripts/'mybox-setup').read_bytes() == (helpers/'mybox-setup').read_bytes()
        for name in ['preinstall', 'postinstall']:
            run('/bin/sh', '-n', str(scripts/name))
        run('/bin/sh', '-n', str(payload/'usr/local/bin/my-container'))
        checks.append('Installed paths are independent of the checkout; Python, source code, personal data, and Apple binaries are absent.')

        # The package must not relocate the bundle to the development .app found
        # through Launch Services. BundleIsRelocatable=false omits relocate entries.
        relocation = info.find('relocate')
        assert relocation is None or len(relocation) == 0
        bom = run('/usr/bin/lsbom', '-p', 'fmu', str(component/'Bom'), text=True).stdout
        for line in bom.splitlines():
            columns = line.split('\t')
            if len(columns) < 3 or '/PrivilegedHelperTools' not in columns[0]:
                continue
            assert columns[2] == '0', line
            assert int(columns[1], 8) & 0o022 == 0, line
        checks.append('The app has a fixed installation location and privileged helper entries are root-owned without group/world write permission.')

        machos = []
        for file in payload.rglob('*'):
            if not file.is_file():
                continue
            with file.open('rb') as stream:
                magic = stream.read(4)
            if magic not in [b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf']:
                continue
            machos.append(file)
            assert run('/usr/bin/lipo', '-archs', str(file), text=True).stdout.strip() == 'arm64', file
            run('/usr/bin/codesign', '--verify', '--strict', str(file))
            if signing['mode'] == 'developer-id':
                verify_code(file, signing['team_id'])
            commands = run('/usr/bin/otool', '-l', str(file), text=True).stdout.splitlines()
            rpaths = [commands[index + 2].strip().removeprefix('path ').rsplit(' (offset ', 1)[0]
                      for index, line in enumerate(commands) if line.strip() == 'cmd LC_RPATH']
            assert all(path == '/usr/lib/swift' for path in rpaths), (str(file), rpaths)
            links = run('/usr/bin/otool', '-L', str(file), text=True).stdout.splitlines()[1:]
            for link in links:
                library = link.strip().split(' (compatibility', 1)[0]
                if library.startswith('@rpath/'):
                    assert rpaths == ['/usr/lib/swift'], (str(file), rpaths)
                    library = '/usr/lib/swift/' + library.removeprefix('@rpath/')
                    # macOS libraries can live in the dyld shared cache rather
                    # than regular files. Verify that the OS loader finds them.
                    ctypes.CDLL(library)
                assert library.startswith(('/System/Library/', '/usr/lib/')), (str(file), library)
        assert len(machos) >= 15, len(machos)
        run('/usr/bin/codesign', '--verify', '--strict', str(app))
        if signing['mode'] == 'developer-id':
            verify_code(app, signing['team_id'])
            verify_code(scripts/'mybox-setup', signing['team_id'])
            checks.append('All executables and the installer setup tool use the same Apple team, hardened runtime, and secure timestamps.')
        for relative, required in [
            ('libexec/container/plugins/container-runtime-linux/bin/container-runtime-linux', ['com.apple.security.virtualization']),
            ('libexec/container/plugins/container-network-vmnet/bin/container-network-vmnet', ['com.apple.security.virtualization']),
        ]:
            response = run('/usr/bin/codesign', '-d', '--entitlements', ':-', str(runtime/relative))
            entitlements = plistlib.loads(response.stdout)
            for key in required:
                assert entitlements.get(key) is True, (relative, key)
        checks.append(f'{len(machos)} arm64 executables have valid local code signatures, system-library dependencies, and the runtime entitlements are present.')

        environment = dict(os.environ, PATH='/usr/bin:/bin:/usr/sbin:/sbin')
        mybox = payload/'usr/local/bin/mybox'
        assert 'mybox' in run(str(mybox), '--version', env=environment, text=True).stdout.lower()
        assert 'Usage: mybox' in run(str(mybox), 'help', env=environment, text=True).stdout
        fork = run(str(runtime/'bin/container'), '--version', env=environment, text=True).stdout.strip()
        assert '1.6.1-mybox.5' in fork, fork
        # Root-only setup entry points must reject a normal account before any
        # account, launchd or firewall operation can happen.
        if os.geteuid() != 0:
            for flag in ['--package-preflight', '--package-install']:
                response = subprocess.run([str(helpers/'mybox-setup'), flag, '/'], env=environment,
                                          capture_output=True, text=True, timeout=10)
                assert response.returncode != 0 and 'administrator authentication' in response.stderr, response.stderr
        checks.append('Extracted CLI and fork report their versions with only macOS system tools on PATH; package setup rejects unprivileged execution.')
    checksum = hashlib.sha256(package.read_bytes()).hexdigest()
    return dict(passed=True, package=str(package.resolve()), sha256=checksum, checks=checks,
                installation_performed=False, guest_boot_tested=False, signing=signing)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path)
    parser.add_argument('--report', type=Path)
    options = parser.parse_args()
    report = verify(options.package.resolve())
    if options.report:
        options.report.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))
