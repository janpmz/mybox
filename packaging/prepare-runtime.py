"""Prepare staged runtime executables without changing the original build."""
from pathlib import Path
import hashlib
import struct
import subprocess
import sys


def run(*arguments):
    return subprocess.run(arguments, check=True, capture_output=True, text=True)


def symbol_tables(binary):
    """Fingerprint every symbol entry and name, including debug/local entries."""
    data = binary.read_bytes()
    position = 32  # The staged runtime contains arm64 Mach-O executables.
    for _ in range(struct.unpack_from('<I', data, 16)[0]):
        command, size = struct.unpack_from('<II', data, position)
        if command == 0x2:  # LC_SYMTAB
            offset, count, strings, string_size = struct.unpack_from('<IIII', data, position + 8)
            assert count > 0 and string_size > 0, binary
            return (count, hashlib.sha256(data[offset:offset + count * 16]).hexdigest(),
                    hashlib.sha256(data[strings:strings + string_size]).hexdigest())
        position += size
    raise AssertionError(f'Runtime symbol tables are missing: {binary}')


for binary in Path(sys.argv[1]).rglob('*'):
    if not binary.is_file():
        continue
    with binary.open('rb') as stream:
        if stream.read(4) != b'\xcf\xfa\xed\xfe':
            continue
    commands = run('/usr/bin/otool', '-l', str(binary)).stdout.splitlines()
    paths = [commands[index + 2].strip().removeprefix('path ').rsplit(' (offset ', 1)[0]
             for index, line in enumerate(commands) if line.strip() == 'cmd LC_RPATH']
    # This package requires macOS 26, which supplies the Swift libraries under
    # /usr/lib/swift. The older deployment target of the standalone runtime also
    # emits toolchain and adjacent-library fallbacks; neither belongs here.
    unwanted = [path for path in paths if path != '/usr/lib/swift']
    symbols_before = symbol_tables(binary)
    dependencies_before = run('/usr/bin/otool', '-L', str(binary)).stdout
    arguments = [value for path in unwanted for value in ['-delete_rpath', path]]
    if arguments:
        run('/usr/bin/install_name_tool', *arguments, str(binary))
    # Keep the complete original symbol tables in the installed programs so
    # crash diagnosis and debugging retain their original detail.
    assert run('/usr/bin/otool', '-L', str(binary)).stdout == dependencies_before, binary
    run('/usr/bin/codesign', '--force', '--sign', '-',
        '--preserve-metadata=identifier,entitlements,flags,runtime', str(binary))
    assert symbol_tables(binary) == symbols_before, binary
    print(f'Prepared {binary.name}: all {symbols_before[0]} symbol entries and names preserved')
