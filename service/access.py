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
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from broker import ACCOUNT, FIREWALL, INSTALL, packet, peer_uid, receive

SOCKET = '/private/var/db/mybox-boot/access.sock'
PREPARATION_SECONDS = 300

# Only the firewall sequence used to prepare a running or stopped box is
# permitted. An interrupted port change may first restore the saved settings.
NEXT_ACTIONS = {
    (): ['apply', 'verify', 'update-ports'],
    ('apply',): ['verify'],
    ('apply', 'verify'): ['verify'],
    ('update-ports',): ['verify'],
}
FINISHED_ACTIONS = {('verify',), ('apply', 'verify', 'verify'), ('update-ports', 'verify')}


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
        packet(conn, ['access-v2', challenge, args])
        answer = receive(conn)
        if answer != ['approved', challenge]:
            raise PermissionError('Box access was not approved.')


def prepare_box(conn, box):
    # This authorization exists only in this root process and connection. No
    # reusable credential is returned to the worker or the terminal client.
    deadline = time.monotonic() + PREPARATION_SECONDS
    expires = time.time() + PREPARATION_SECONDS
    actions, used, policy = (), set(), None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PermissionError('Box preparation approval expired.')
        conn.settimeout(remaining)
        try:
            message = receive(conn)
        except EOFError:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PermissionError('Box preparation approval expired.')
        if message == ['finish']:
            if actions not in FINISHED_ACTIONS:
                raise PermissionError('Firewall preparation is incomplete.')
            packet(conn, ['finished'])
            return
        if (not isinstance(message, list) or len(message) != 2 or message[0] != 'firewall'
                or not isinstance(message[1], str) or actions not in NEXT_ACTIONS):
            raise ValueError('Unexpected approved firewall operation.')
        token = message[1]
        if str(uuid.UUID(token)) != token or token in used:
            raise ValueError('Invalid or reused firewall request.')
        used.add(token)
        scope = dict(box=box, expires=expires, actions=NEXT_ACTIONS[actions], policy=policy)
        # Fixed root-owned executable, fixed operation, private stdin. The
        # helper checks the actual request under its lock before changing PF.
        try:
            result = subprocess.run([FIREWALL, 'approve-access', token],
                                    input=json.dumps(scope).encode(), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=remaining, check=True)
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or b'').decode(errors='replace').strip()
            detail = ''.join(c if c.isprintable() or c in '\n\t' else '?' for c in detail)[:2000]
            detail = detail or f'The firewall helper exited with status {error.returncode}.'
            print(detail, file=sys.stderr, flush=True)
            packet(conn, ['firewall-error', token, detail])
            return
        except subprocess.TimeoutExpired:
            packet(conn, ['firewall-error', token, 'Firewall preparation timed out; entry was refused.'])
            return
        confirmed = json.loads(result.stdout)
        if (not isinstance(confirmed, dict) or confirmed.get('action') not in NEXT_ACTIONS[actions]
                or not isinstance(confirmed.get('policy'), dict)
                or confirmed['policy'].get('network') != 'mybox-net-' + box
                or (policy is not None and confirmed['policy'] != policy)):
            raise PermissionError('The firewall helper returned an unexpected confirmation.')
        policy = confirmed['policy']
        actions += (confirmed['action'],)
        if time.monotonic() >= deadline:
            raise PermissionError('Box preparation approval expired.')
        packet(conn, ['firewall', token])


def serve(conn):
    if os.geteuid() != 0 or peer_uid(conn) != pwd.getpwnam(ACCOUNT).pw_uid:
        raise PermissionError('Only the private MyBox service can request approval.')
    conn.settimeout(5)
    message = receive(conn)
    if not isinstance(message, list) or len(message) != 3 or message[0] != 'access-v2':
        raise ValueError('Update the MyBox worker and approval service together.')
    _, challenge, args = message
    if not isinstance(challenge, str) or not re.fullmatch('[0-9a-f]{64}', challenge):
        raise ValueError('Invalid approval challenge.')
    reason = prompt_for(args) + ' Also prepare and check this box’s Mac firewall protection.'
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
    prepare_box(conn, args[1])


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
