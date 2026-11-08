"""Check service coexistence using empty temporary stores; never start a VM.

Pass the original/legacy CLI and the relocated fork's real executable paths.
Both services must be stopped in the calling account before this check.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('original', type=Path)
    parser.add_argument('fork', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    programs = dict(original=args.original.resolve(), fork=args.fork.resolve())
    directory = Path(tempfile.mkdtemp(prefix='mybox-coexistence-')).resolve()
    environment = dict(os.environ, XDG_CONFIG_HOME=str(directory / 'config'))
    for key in ['CONTAINER_APP_ROOT', 'CONTAINER_INSTALL_ROOT', 'CONTAINER_DISABLE_HOST_DNS']:
        environment.pop(key, None)
    results = dict(temporary_directory=str(directory), versions={}, checks=[])
    started = set()

    def run(which, *arguments, check=True):
        response = subprocess.run([programs[which], *arguments], env=environment,
                                  capture_output=True, text=True, timeout=90)
        if check and response.returncode:
            raise RuntimeError(f'{which} {arguments}: {response.stderr or response.stdout}')
        return response

    def status(which):
        return json.loads(run(which, 'system', 'status', '--format', 'json', check=False).stdout)

    def check_running(which):
        state = status(which)
        assert state['status'] == 'running', state
        assert Path(state['paths']['appRoot']).resolve() == directory / which, state
        assert Path(state['paths']['installRoot']).resolve() == programs[which].parent.parent, state
        assert json.loads(run(which, 'list', '--all', '--format', 'json').stdout) == []
        return state

    def start(which):
        # Mark first so cleanup also handles a partially successful startup.
        started.add(which)
        run(which, 'system', 'start', '--app-root', str(directory / which),
            '--install-root', str(programs[which].parent.parent), '--disable-kernel-install')
        check_running(which)

    try:
        for which in programs:
            state = status(which)
            assert state['status'] == 'unregistered', f'{which} is already active; refusing to interrupt it: {state}'
            results['versions'][which] = run(which, '--version').stdout.strip()
        start('original')
        start('fork')
        results['checks'].append('Both runtimes list their own empty stores while running together.')
        run('fork', 'system', 'stop')
        assert status('fork')['status'] == 'unregistered'
        started.remove('fork')
        check_running('original')
        results['checks'].append('Stopping the fork leaves the original service running.')
        start('fork')
        run('original', 'system', 'stop')
        assert status('original')['status'] == 'unregistered'
        started.remove('original')
        check_running('fork')
        results['checks'].append('Stopping the original leaves the fork service running.')
        results['passed'] = True
    finally:
        for which in list(started):
            run(which, 'system', 'stop', check=False)
            if status(which)['status'] == 'unregistered':
                started.remove(which)
        results['cleanup_complete'] = not started
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(results, indent=2) + '\n')
        # Preserve logs for review; no existing user data is touched.
        print(json.dumps(results, indent=2))
        if started:
            raise RuntimeError(f'Test services still need cleanup: {sorted(started)}')


if __name__ == '__main__':
    main()
