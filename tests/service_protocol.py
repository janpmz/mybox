"""Independent wire-protocol client for native-service and live tests only."""
import json
import socket
import struct
from pathlib import Path
from contextlib import contextmanager

ACCOUNT = '_mybox'
HOME = Path('/private/var/db/mybox-runtime')
INSTALL = Path('/Library/PrivilegedHelperTools/io.github.janpmz.my-container-box.service')
SOCKET = '/private/var/db/mybox-socket/service.sock'
FIREWALL = '/Library/PrivilegedHelperTools/mybox-firewall-manager'
COMMANDS = {'list', 'create', 'copy', 'start', 'stop', 'delete', 'enter', 'ports', 'resize',
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
