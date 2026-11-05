"""Resize a disposable box on Apple's backend. Keep it offline for every boot."""
import json
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 ** 2
GB = 1_000_000_000


def allocation_bytes(requested):
    return ((requested + MIB - 1) // MIB) * MIB


def run(*args, check=True, answers=None):
    result = subprocess.run(args, text=True, input=answers, capture_output=True, timeout=180)
    if check and result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result


def main():
    name = 'resize-check-' + uuid.uuid4().hex[:12]
    cid = 'mybox-' + name
    box = ROOT / '.build/mybox-test'
    bundle = Path.home() / 'Library/Application Support/com.apple.container/containers' / cid
    disk = bundle / 'rootfs.ext4'
    checks = []
    run('container', 'create', '--name', cid, '--label', 'io.github.janpmz.my-container-box.managed=true',
        '--network', 'none', '--cpus', '2', '--memory', '512m', '--entrypoint', '/bin/sleep',
        'docker.io/library/debian:trixie-slim', 'infinity')
    try:
        def guest(script):
            return run('container', 'exec', cid, '/bin/sh', '-ec', script).stdout

        run(box, 'resize', name, '--cpus', '1', '--memory-gb', '0.375')
        run('container', 'start', cid)
        quota, period = map(int, guest('cat /sys/fs/cgroup/cpu.max').split())
        assert quota == period
        assert int(guest('cat /sys/fs/cgroup/memory.max')) == 384 * MIB
        guest('''mkdir -p /workspace
            cd /workspace
            printf resize-test > data
            chmod 640 data
            chown 123:456 data
            ln data hardlink
            ln -s data link
            mkfifo fifo
            truncate -s 2G sparse''')
        checks.append('CPU and memory settings apply on first boot')
        for cpus, memory_gb, size in [(3, 0.75, 1), (1, 0.25, 2)]:
            run('container', 'stop', cid)
            memory_bytes = int(memory_gb * 1024 ** 3)
            disk_bytes = allocation_bytes(size * GB)
            if size == 1:
                result = run(box, 'resize', name, answers=f'{cpus}\n{memory_gb}\n{size}\n')
                assert 'Disk in GB [' in result.stderr
            else:
                run(box, 'resize', name, '--cpus', str(cpus), '--memory-gb', str(memory_gb), '--disk-size-gb', str(size))
            assert disk.stat().st_size == disk_bytes
            row = next(line for line in run(box, 'list').stdout.splitlines() if line.startswith(name + ' '))
            assert 'stopped' in row and f'{memory_gb} GB' in row.replace(',', '.') and f'{size} GB' in row, row
            run('container', 'start', cid)
            quota, period = map(int, guest('cat /sys/fs/cgroup/cpu.max').split())
            assert quota == cpus * period
            assert int(guest('cat /sys/fs/cgroup/memory.max')) == memory_bytes
            total = int(guest('df -B1 --output=size / | tail -1'))
            assert disk_bytes * 0.85 < total <= disk_bytes
            guest('''cd /workspace
                test "$(cat data)" = resize-test
                test "$(stat -c '%a %u %g %h' data)" = '640 123 456 2'
                test hardlink -ef data
                test "$(readlink link)" = data
                test -p fifo
                test "$(stat -c '%s %b' sparse)" = '2147483648 0' ''')
            checks.append(dict(cpus=cpus, memoryGB=memory_gb, diskGB=size, filesystemBytes=total,
                               files='content, ownership, mode, hard link, symlink, FIFO, and sparse allocation matched'))
        guest('yes preserve-this-data | head -c 96M > /workspace/fill')
        run('container', 'stop', cid)
        original = disk.stat().st_ino, (bundle / 'config.json').read_bytes()
        result = run(box, 'resize', name, '--memory-gb', '0.5', '--disk-size-gb', '0.134217728', check=False)
        assert result.returncode != 0 and 'No space left' in result.stderr, result.stderr
        assert original == (disk.stat().st_ino, (bundle / 'config.json').read_bytes())
        run('container', 'start', cid)
        guest('test "$(cat /workspace/data)" = resize-test; test "$(stat -c %s /workspace/fill)" = 100663296')
        checks.append('Insufficient space preserved the original disk and configuration. The box restarted.')
    finally:
        run('container', 'stop', cid, check=False)
        run('container', 'delete', cid)
    report = json.dumps(dict(backend=run('container', '--version').stdout.strip(), status='passed', checks=checks), indent=2)
    (ROOT / 'test-results/resize-latest.json').write_text(report + '\n')
    print(report)


if __name__ == '__main__':
    main()
