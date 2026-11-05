"""Serve MyBox commands as the private account. Run Python in isolated mode."""
import array
import ctypes
from contextlib import contextmanager
import json
import os
from pathlib import Path
import pwd
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from transfer import checked_name, pack, unpack

ACCOUNT = '_mybox'
HOME = Path('/private/var/db/mybox-runtime')
INSTALL = Path('/Library/PrivilegedHelperTools/io.github.janpmz.my-container-box.service')
SOCKET = '/private/var/db/mybox-socket/service.sock'
FIREWALL = '/Library/PrivilegedHelperTools/mybox-firewall-manager'
COMMANDS = {'list', 'create', 'copy', 'start', 'stop', 'enter', 'ports', 'resize',
            'import', 'export', '_complete-boxes'}
MAX_REQUEST = 128 * 1024


@contextmanager
def socket_pair():
    first, second = socket.socketpair()
    with first, second:
        yield first, second


def packet(connection, value):
    data = json.dumps(value).encode('ascii')
    if len(data) > MAX_REQUEST:
        raise ValueError('The command is too long.')
    connection.sendall(struct.pack('!I', len(data)) + data)


def receive(connection):
    def read(size):
        data = bytearray()
        while len(data) < size:
            chunk = connection.recv(size - len(data))
            if not chunk:
                raise EOFError('The MyBox connection closed.')
            data.extend(chunk)
        return data

    size, = struct.unpack('!I', read(4))
    if size > MAX_REQUEST:
        raise ValueError('The command is too long.')
    return json.loads(read(size))


def peer_uid(connection):
    # LOCAL_PEERCRED returns the kernel's xucred structure on macOS.
    version, uid = struct.unpack_from('=II', connection.getsockopt(0, 1, 128))
    if version != 0:
        raise ValueError('Unknown macOS peer credential format.')
    return uid


def receive_fds(connection):
    width = array.array('i').itemsize
    marker, ancillary, flags, _ = connection.recvmsg(1, socket.CMSG_SPACE(4 * width))
    descriptors = []
    for level, kind, data in ancillary:
        if (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS):
            group = array.array('i')
            group.frombytes(data)
            descriptors.extend(group)
    if marker != b'F' or flags & socket.MSG_CTRUNC or len(descriptors) != 4:
        for fd in descriptors:
            os.close(fd)
        raise ValueError('Invalid command streams.')
    return descriptors


def validate_command(args):
    if (not isinstance(args, list) or not args or
        any(not isinstance(arg, str) or '\0' in arg for arg in args) or args[0] not in COMMANDS):
        raise ValueError('Unsupported MyBox command.')
    if args[0] == 'import':
        if len(args) != 3:
            raise ValueError('Usage: mybox import <box> <host-path>')
        checked_name(args[2])
    if args[0] == 'export' and len(args) != 3:
        raise ValueError('Usage: mybox export <box> <box-path> <new-host-path>')


def run_worker(args, descriptors, connection, worker):
    # Only the firewall challenge and its reply cross this channel. The worker
    # verifies the root-owned receipt separately before it can start Linux.
    with socket_pair() as (local, remote):
        env = dict(os.environ, MYBOX_FIREWALL_CHANNEL=str(remote.fileno()))
        proc = subprocess.Popen([worker, *args], stdin=descriptors[0], stdout=descriptors[1],
                                stderr=descriptors[2], env=env, pass_fds=(remote.fileno(),),
                                start_new_session=True)
        remote.close()
        try:
            with selectors.DefaultSelector() as poll:
                poll.register(local, selectors.EVENT_READ)
                poll.register(connection, selectors.EVENT_READ)
                pending = False
                while proc.poll() is None:
                    for key, _ in poll.select(0.15):
                        if key.fileobj is local:
                            if not local.recv(1, socket.MSG_PEEK):
                                poll.unregister(local)
                                continue
                            # The worker writes a fixed-size UUID instead of JSON.
                            token = bytearray()
                            while len(token) < 36:
                                chunk = local.recv(36 - len(token))
                                if not chunk:
                                    raise EOFError('Incomplete firewall request.')
                                token.extend(chunk)
                            if pending:
                                raise ValueError('Unexpected firewall request.')
                            packet(connection, ['firewall', token.decode('ascii')])
                            pending = True
                        else:
                            reply = receive(connection)
                            if pending and reply in (['firewall', True], ['firewall', False]):
                                pending = False
                                local.sendall(b'Y' if reply[1] else b'N')
                            elif reply == ['signal', 'WINCH']:
                                os.killpg(proc.pid, signal.SIGWINCH)
                            elif reply == ['signal', 'INT']:
                                os.killpg(proc.pid, signal.SIGINT)
                            else:
                                raise ValueError('Unexpected client reply.')
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        return proc.returncode if proc.returncode >= 0 else 128 - proc.returncode


def serve_one(conn, owner, worker):
    if peer_uid(conn) != owner:
        raise PermissionError('This Mac account does not own the MyBox installation.')
    args = receive(conn)
    validate_command(args)
    fds = receive_fds(conn)
    try:
        with tempfile.TemporaryDirectory() as temp, os.fdopen(fds[3], 'r+b', closefd=False) as transfer:
            payload = Path(temp) / 'payload'
            if args[0] == 'import':
                # A separate directory avoids a collision when the chosen name is "payload".
                destination = Path(temp) / 'input'
                destination.mkdir()
                unpack(transfer, temp).rename(destination / args[2])
                args[2] = str(destination / args[2])
            elif args[0] == 'export':
                args.append(str(payload))
            status = run_worker(args, fds, conn, worker)
            if status == 0 and args[0] == 'export':
                pack(payload, transfer)
            packet(conn, ['exit', status])
    finally:
        for fd in fds:
            os.close(fd)


def serve():
    account = pwd.getpwnam(ACCOUNT)
    if os.getuid() != account.pw_uid or os.geteuid() != account.pw_uid or account.pw_uid == 0:
        raise PermissionError('The box service requires its dedicated non-admin account.')
    if Path(account.pw_dir) != HOME or stat.S_IMODE(HOME.stat().st_mode) != 0o700 or HOME.stat().st_uid != account.pw_uid:
        raise PermissionError('The service home directory must be private.')
    session = subprocess.check_output(['/bin/launchctl', 'managername'], text=True).strip()
    if session != 'Background':
        raise ValueError('The box service needs its own macOS background user session.')
    owner = int((INSTALL / 'owner').read_text().strip())
    os.umask(0o077)
    worker = str(INSTALL / 'mybox-worker')
    # The root-owned parent of this directory prevents replacement by a client.
    if os.path.lexists(SOCKET):
        os.unlink(SOCKET)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(SOCKET)
        os.chmod(SOCKET, 0o666)
        listener.listen(32)
        # Handle each connection in an independent interpreter without forking
        # a process that already uses Apple's system frameworks.
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        while True:
            conn, _ = listener.accept()
            with conn:
                if peer_uid(conn) == owner:
                    subprocess.Popen(['/usr/bin/python3', '-I', '-B', str(HERE / 'broker.py'), 'session',
                                      str(conn.fileno()), str(owner), worker], pass_fds=(conn.fileno(),))


def publish(source, destination):
    # Both paths are on the destination volume. RENAME_EXCL forbids replacement.
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    if libc.renamex_np(os.fsencode(source), os.fsencode(destination), 4) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def client(args):
    if not args:
        raise ValueError('Missing MyBox command.')
    destination = None
    with tempfile.TemporaryFile() as transfer, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        try:
            conn.connect(SOCKET)
        except OSError as error:
            raise ValueError('The private MyBox service is unavailable. Run mybox setup-service in your Mac terminal.') from error
        if peer_uid(conn) != pwd.getpwnam(ACCOUNT).pw_uid:
            raise PermissionError('The connection did not reach the private MyBox account.')
        skipped = 0
        if args[0] == 'import' and len(args) == 3:
            path = Path(args[2]).expanduser().absolute()
            if path.resolve() in (Path('/'), Path.home().resolve()):
                raise ValueError('Choose a project file or directory.')
            args[2] = checked_name(path.name)
            skipped = pack(path, transfer, skip_special=True)
            transfer.seek(0)
        if args[0] == 'export':
            if len(args) != 4:
                raise ValueError('Usage: mybox export <box> <box-path> <new-host-path>')
            destination = Path(args.pop()).expanduser().absolute()
            if os.path.lexists(destination) or not destination.parent.is_dir():
                raise ValueError('Choose a new destination in an existing directory.')
        validate_command(args)
        packet(conn, args)
        conn.sendmsg([b'F'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [0, 1, 2, transfer.fileno()]))])
        # A self-pipe keeps signal messages out of partially written command frames.
        with socket_pair() as (events, wake), selectors.DefaultSelector() as poll:
            wake.setblocking(False)
            previous = signal.set_wakeup_fd(wake.fileno())
            watched = (signal.SIGWINCH, signal.SIGINT)
            handlers = {sig: signal.signal(sig, lambda *_: None) for sig in watched}
            try:
                poll.register(conn, selectors.EVENT_READ)
                poll.register(events, selectors.EVENT_READ)
                while True:
                    for key, _ in poll.select():
                        if key.fileobj is events:
                            for sig in events.recv(256):
                                if sig in watched:
                                    packet(conn, ['signal', 'INT' if sig == signal.SIGINT else 'WINCH'])
                            continue
                        message = receive(conn)
                        if message[0] == 'exit':
                            status = message[1]
                            if status == 0 and destination is not None:
                                transfer.seek(0)
                                with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
                                    publish(unpack(transfer, temp), destination)
                                print('Exported to', destination)
                            if status == 0 and skipped:
                                print(f'Skipped {skipped} symlinks or special files.')
                            return status
                        if message[0] == 'error':
                            raise ValueError(message[1])
                        if message[0] != 'firewall' or len(message) != 2:
                            raise ValueError('Unexpected service reply.')
                        token = str(uuid.UUID(message[1]))
                        result = subprocess.run(['/usr/bin/sudo', FIREWALL, 'approve', token],
                                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
                        packet(conn, ['firewall', result.returncode == 0])
            finally:
                signal.set_wakeup_fd(previous)
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)


if __name__ == '__main__':
    try:
        if sys.argv[1:2] == ['serve']:
            serve()
        elif sys.argv[1:2] == ['session']:
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)
            with socket.socket(fileno=int(sys.argv[2])) as connection:
                try:
                    serve_one(connection, int(sys.argv[3]), sys.argv[4])
                except Exception as error:
                    packet(connection, ['error', str(error)])
        elif sys.argv[1:2] == ['client']:
            sys.exit(client(sys.argv[2:]))
        else:
            raise ValueError('Unknown service operation.')
    except (OSError, EOFError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('MyBox:', error, file=sys.stderr)
        sys.exit(1)
