"""Install the MyBox account and its background service on a development Mac."""
import argparse
import grp
import os
from pathlib import Path
import plistlib
import pwd
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid

ACCOUNT = '_mybox'
HOME = Path('/private/var/db/mybox-runtime')
SOCKET_DIR = Path('/private/var/db/mybox-socket')
INSTALL = Path('/Library/PrivilegedHelperTools/io.github.janpmz.my-container-box.service')
BOOT = Path('/Library/LaunchDaemons/io.github.janpmz.my-container-box.bootstrap.plist')
BASE = Path(__file__).resolve().parent.parent
DSCL = '/usr/bin/dscl'
LAUNCH = '/bin/launchctl'


def command(*args, check=True):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f'{" ".join(args)} failed. {result.stderr.strip() or result.stdout.strip()}')
    return result


def directory(path, uid, gid, mode):
    try:
        path.mkdir(mode=mode)
    except FileExistsError:
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_gid != gid or
                stat.S_IMODE(info.st_mode) != mode):
            raise ValueError(f'Unexpected owner or permissions on {path}.')
    else:
        os.chown(path, uid, gid)
        path.chmod(mode)


def current_file(path, content, mode):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
        raise ValueError(f'Unexpected ownership or file type at {path}.')
    return stat.S_IMODE(info.st_mode) == mode and path.read_bytes() == content


def store(path, content, mode):
    if current_file(path, content, mode):
        return False
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        temp = Path(out.name)
        try:
            out.write(content)
            out.flush()
            os.fchmod(out.fileno(), mode)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return True


def check_service_id(number):
    users = {entry.pw_name for entry in pwd.getpwall() if entry.pw_uid == number}
    groups = {entry.gr_name for entry in grp.getgrall() if entry.gr_gid == number}
    if (users | groups) - {ACCOUNT}:
        raise ValueError(f'The service ID {number} is already in use by another account or group.')


def install_identity():
    metadata = INSTALL / 'identity.plist'
    if metadata.exists():
        identity = plistlib.loads(metadata.read_bytes())
    else:
        for lookup in (pwd.getpwnam, grp.getgrnam):
            try:
                lookup(ACCOUNT)
            except KeyError:
                continue
            raise ValueError(f'An unrelated account or group already uses {ACCOUNT}.')
        used = {entry.pw_uid for entry in pwd.getpwall()} | {entry.gr_gid for entry in grp.getgrall()}
        number = next((i for i in range(499, 299, -1) if i not in used), None)
        if number is None:
            raise ValueError('No unused service ID is available.')
        identity = dict(number=number, user=str(uuid.uuid4()), group=str(uuid.uuid4()))
        store(metadata, plistlib.dumps(identity), 0o600)

    return configure_identity(identity)


def configure_identity(identity):
    number = identity['number']
    check_service_id(number)
    for kind, key, id_key, attributes in [
        ('Groups', 'group', 'PrimaryGroupID', {'RealName': 'MyBox service group'}),
        ('Users', 'user', 'UniqueID', {'PrimaryGroupID': str(number), 'RealName': 'MyBox service',
         'NFSHomeDirectory': str(HOME), 'UserShell': '/usr/bin/false', 'IsHidden': '1', 'Password': '*'})]:
        path = f'/{kind}/{ACCOUNT}'
        record = command(DSCL, '.', '-read', path, 'GeneratedUID', check=False)
        existing = record.returncode == 0
        if existing:
            if record.stdout.split()[-1].lower() != identity[key].lower():
                raise ValueError(f'The existing {path} belongs to another installation.')
        else:
            if 'eDSRecordNotFound' not in (record.stdout + record.stderr):
                raise RuntimeError(f'Cannot check the directory record {path}: {record.stderr or record.stdout}')
            command(DSCL, '.', '-create', path, 'GeneratedUID', identity[key])
        for attribute, value in {id_key: str(number), **attributes}.items():
            if existing:
                # A reinstall must never reset an existing account password.
                # A new record has no usable password before initial setup completes.
                if attribute in ('Password', 'RealName'):
                    continue
                current = command(DSCL, '.', '-read', path, attribute, check=False)
                prefix = attribute + ': '
                found = [line.split(prefix, 1)[1] for line in current.stdout.splitlines() if prefix in line]
                if found == [value]:
                    continue
                if found or (current.returncode and 'eDSAttributeNotFound' not in current.stdout + current.stderr):
                    raise ValueError(f'The existing {path} has an unexpected {attribute}.')
            command(DSCL, '.', '-create', path, attribute, value)
    account = pwd.getpwnam(ACCOUNT)
    if account.pw_uid != number or account.pw_gid != number or account.pw_dir != str(HOME):
        raise ValueError('The Mac returned unexpected details for the service account.')
    if 'admin' in command('/usr/bin/id', '-Gn', ACCOUNT).stdout.split():
        raise ValueError('The MyBox service account must not be an administrator.')
    return number


def install_socket_service(uid, suffix, socket_name, program):
    guard = Path('/private/var/db/mybox-boot')
    directory(guard, 0, uid, 0o750)
    label = 'io.github.janpmz.my-container-box.' + suffix
    plist = Path('/Library/LaunchDaemons') / (label + '.plist')
    job = dict(Label=label, ProgramArguments=program,
               inetdCompatibility={'Wait': False},
               Sockets={'Listener': {'SockPathName': str(guard / socket_name), 'SockPathMode': 0o666}},
               StandardErrorPath='/var/log/mybox-firewall.log')
    changed = store(plist, plistlib.dumps(job), 0o644)
    loaded = command(LAUNCH, 'print', 'system/' + label, check=False).returncode == 0
    if changed or not loaded:
        if loaded:
            command(LAUNCH, 'bootout', 'system/' + label)
        command(LAUNCH, 'bootstrap', 'system', str(plist))


def main():
    parser = argparse.ArgumentParser()
    parser.parse_args()
    owner = int(os.environ.get('SUDO_UID', '0'))
    if os.geteuid() != 0 or owner <= 0:
        raise ValueError('Run mybox setup-service from your normal Mac account.')
    os.umask(0o077)
    # Privileged copies come from the checkout explicitly selected at setup.
    # Require the shared fork before configuring its firewall and account.
    command('/usr/sbin/pkgutil', '--pkg-info', 'io.github.janpmz.my-container-box')
    for path in ['/usr/local', '/usr/local/bin', '/usr/local/bin/container', '/usr/local/bin/container-apiserver',
                 '/usr/local/libexec', '/usr/local/libexec/container',
                 '/usr/local/libexec/container/container-mybox-bridge', '/Library/PrivilegedHelperTools']:
        info = os.lstat(path)
        if info.st_uid != 0 or stat.S_ISLNK(info.st_mode) or info.st_mode & 0o022:
            raise ValueError(f'The runtime installation must be under administrator control: {path}')
    runtime_files = Path('/usr/local/libexec/container').rglob('*')
    for path in runtime_files:
        info = path.lstat()
        if info.st_uid != 0 or stat.S_ISLNK(info.st_mode) or info.st_mode & 0o022:
            raise ValueError(f'Unexpected permissions on an Apple runtime component: {path}')
    artifacts = {'mybox-authenticate': BASE / '.build/mybox-authenticate',
                 'access.py': BASE / 'service/access.py', 'mybox-worker': BASE / '.build/mybox-worker',
                 'mybox-port-forwarder': BASE / '.build/mybox-port-forwarder',
                 'transfer.py': BASE / 'service/transfer.py', 'broker.py': BASE / 'service/broker.py'}
    firewall = BASE / '.build/mybox-firewall-manager'
    for source in [*artifacts.values(), firewall]:
        if not source.is_file():
            raise ValueError(f'Missing {source}. Run make build first.')
    directory(INSTALL, 0, 0, 0o755)
    import fcntl
    with (INSTALL / 'setup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (INSTALL / 'owner').exists() and (INSTALL / 'owner').read_text().strip() != str(owner):
            raise ValueError('Another Mac account owns this installation.')
        uid = install_identity()
        directory(HOME, uid, uid, 0o700)
        directory(HOME / 'requests', uid, uid, 0o700)
        directory(SOCKET_DIR, uid, uid, 0o755)
        reload_broker = False
        for name, source in artifacts.items():
            changed = store(INSTALL / name, source.read_bytes(), 0o644 if name.endswith('.py') else 0o755)
            if changed and name.endswith('.py'):
                reload_broker = True
        store(INSTALL / 'owner', f'{owner}\n'.encode(), 0o644)
        install_socket_service(uid, 'verify-boot', 'verify.sock',
                               ['/Library/PrivilegedHelperTools/mybox-firewall-manager', 'verify-runtime-boot'])
        install_socket_service(uid, 'approve-access', 'access.sock',
                               ['/usr/bin/python3', '-I', '-B', str(INSTALL / 'access.py'), 'serve'])
        # This is the existing privileged firewall installation. Only it handles PF.
        firewall_path = Path('/Library/PrivilegedHelperTools/mybox-firewall-manager')
        if (not current_file(firewall_path, firewall.read_bytes(), 0o755) or
                command(LAUNCH, 'print', 'system/io.github.janpmz.my-container-box.firewall', check=False).returncode):
            subprocess.run([firewall, 'install'], check=True)
        broker = dict(Label='io.github.janpmz.my-container-box.service', LimitLoadToSessionType='Background',
                      ProgramArguments=['/usr/bin/python3', '-I', '-B', str(INSTALL / 'broker.py'), 'serve'],
                      WorkingDirectory=str(HOME), KeepAlive=True,
                      EnvironmentVariables={'HOME': str(HOME), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin'})
        agent = INSTALL / 'service.plist'
        reload_broker = store(agent, plistlib.dumps(broker), 0o644) or reload_broker
        # launchctl establishes the user's background domain during boot. Only
        # this registration runs as root. launchd runs the broker as the domain UID.
        domain = f'user/{uid}'
        bootstrap = dict(Label='io.github.janpmz.my-container-box.bootstrap', RunAtLoad=True,
                         ProgramArguments=[LAUNCH, 'asuser', str(uid), LAUNCH, 'bootstrap', domain, str(agent)])
        changed_boot = store(BOOT, plistlib.dumps(bootstrap), 0o644)
        registered = command(LAUNCH, 'print', domain + '/io.github.janpmz.my-container-box.service', check=False).returncode == 0
        boot_registered = command(LAUNCH, 'print', 'system/io.github.janpmz.my-container-box.bootstrap', check=False).returncode == 0
        if reload_broker or changed_boot or not registered or not boot_registered:
            command(LAUNCH, 'bootout', 'system/io.github.janpmz.my-container-box.bootstrap', check=False)
            command(LAUNCH, 'bootout', domain + '/io.github.janpmz.my-container-box.service', check=False)
            command(LAUNCH, 'bootstrap', 'system', str(BOOT))
        for _ in range(100):
            registered = command(LAUNCH, 'print', domain + '/io.github.janpmz.my-container-box.service', check=False)
            if registered.returncode == 0:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    if probe.connect_ex(str(SOCKET_DIR / 'service.sock')) == 0:
                        break
            time.sleep(.1)
        else:
            raise RuntimeError(f'The service did not start in {domain}. Inspect launchctl print system/io.github.janpmz.my-container-box.bootstrap.')
    print('Installed the private MyBox account. Check the connection with mybox list.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit(f'MyBox setup: {error}')
