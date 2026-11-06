"""Enter an existing box once and fail if the terminal is asked to run sudo.

Run after setup-service. Approve the macOS prompt for /bin/pwd. No container
files are changed; a stopped box will be started by the usual entry path.
"""
import argparse
import array
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'service'))
import broker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('box')
    args = parser.parse_args()
    print(f'Approve the MyBox prompt to run /bin/pwd in {args.box}.', flush=True)
    with socket.socket(socket.AF_UNIX) as conn, open(os.devnull, 'rb') as empty, \
         tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err, tempfile.TemporaryFile() as transfer:
        conn.settimeout(150)
        conn.connect(broker.SOCKET)
        broker.packet(conn, ['enter', args.box, '--', '/bin/pwd'])
        descriptors = array.array('i', [empty.fileno(), out.fileno(), err.fileno(), transfer.fileno()])
        conn.sendmsg([b'F'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptors)])
        answer = broker.receive(conn)
        if answer[0] == 'firewall':
            broker.packet(conn, ['firewall', False])
            raise RuntimeError('Entry still requested terminal sudo after access approval.')
        out.seek(0)
        err.seek(0)
        output, errors = out.read(), err.read().decode(errors='replace')
        if answer != ['exit', 0] or output != b'/workspace\n':
            raise RuntimeError(f'Entry failed: {answer!r}, output={output!r}, errors={errors}')
    report = dict(finished_at=datetime.now(timezone.utc).isoformat(), box=args.box,
                  command=['/bin/pwd'], stdout=output.decode(), terminal_sudo_requests=0,
                  passed=True)
    (ROOT / 'test-results/access-approval-latest.json').write_text(json.dumps(report, indent=2) + '\n')
    print('Passed: authenticated entry reached /workspace without requesting terminal sudo.')


if __name__ == '__main__':
    main()
