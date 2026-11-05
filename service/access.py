"""Get a fresh macOS approval for one request from the private MyBox worker."""
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import socket
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from broker import ACCOUNT, INSTALL, packet, peer_uid, receive

SOCKET = '/private/var/db/mybox-boot/access.sock'


def prompt_for(args):
    if (not isinstance(args, list) or len(args) < 2 or
            not all(isinstance(arg, str) for arg in args) or
            not re.fullmatch('[a-z][a-z0-9-]{0,47}', args[1])):
        raise ValueError('Invalid box access request.')
    action, box, *values = args
    # Keep argument boundaries and escape controls, bidi marks and line breaks.
    shown = ' '.join(json.dumps(value, ensure_ascii=True) for value in values)
    if action == 'enter' and not values:
        reason = f'Open a shell in box “{box}”. The requesting terminal controls this shell.'
    elif action == 'enter' and values:
        reason = f'Run in box “{box}”. The requesting program controls input and receives output. Command: {shown}'
    elif action == 'export' and len(values) == 1:
        reason = f'Give the requesting program a copy of {shown} from box “{box}”.'
    elif action == 'import' and len(values) == 1:
        reason = f'Allow the requesting program to write {shown} into box “{box}”.'
    else:
        raise ValueError('Unsupported box access request.')
    return reason


def request(args):
    # Every operation gets its own connection and random challenge. Neither is
    # exposed to the CLI client. There is no receipt to cache between requests.
    challenge = secrets.token_hex(32)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(125)
        conn.connect(SOCKET)
        if peer_uid(conn) != 0:
            raise PermissionError('The macOS approval service is unavailable.')
        packet(conn, [challenge, args])
        answer = receive(conn)
        if answer != ['approved', challenge]:
            raise PermissionError('Box access was not approved.')


def serve(conn):
    if os.geteuid() != 0 or peer_uid(conn) != pwd.getpwnam(ACCOUNT).pw_uid:
        raise PermissionError('Only the private MyBox service can request approval.')
    conn.settimeout(5)
    challenge, args = receive(conn)
    if not isinstance(challenge, str) or not re.fullmatch('[0-9a-f]{64}', challenge):
        raise ValueError('Invalid approval challenge.')
    reason = prompt_for(args)
    owner = int((INSTALL / 'owner').read_text())
    if owner <= 0 or os.stat('/dev/console').st_uid != owner:
        raise PermissionError('Sign in to the Mac account that installed MyBox to approve access.')
    # Only this root-owned service launches the authentication program and reads
    # its response. The ordinary CLI cannot substitute its own approval.
    result = subprocess.run(['/bin/launchctl', 'asuser', str(owner),
                             '/usr/bin/sudo', '-n', '-H', '-u', f'#{owner}', '--',
                             str(INSTALL / 'mybox-authenticate')],
                            input=json.dumps([challenge, reason]).encode(),
                            stdout=subprocess.PIPE, timeout=115)
    if result.returncode or result.stdout != challenge.encode():
        raise PermissionError('Box access was not approved.')
    packet(conn, ['approved', challenge])


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['serve']:
            with socket.socket(fileno=0) as client:
                try:
                    serve(client)
                except (OSError, ValueError, EOFError, subprocess.SubprocessError):
                    packet(client, ['denied'])
        elif sys.argv[1:2] == ['request']:
            request(sys.argv[2:])
        else:
            raise ValueError('Unknown box approval operation.')
    except (OSError, EOFError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(f'MyBox: {error}')
