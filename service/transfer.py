"""Transfer ordinary files and directories through an already-open file descriptor."""
import json
import os
from pathlib import Path
import stat
import struct


def checked_name(name):
    if not isinstance(name, str) or not name or name in ('.', '..') or '/' in name or '\0' in name:
        raise ValueError('Invalid file name in transfer.')
    if len(os.fsencode(name)) > 255:
        raise ValueError('File name is too long.')
    return name


def exact(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise ValueError('Incomplete file transfer.')
    return data


def header(stream, value):
    data = json.dumps(value).encode('ascii')
    stream.write(struct.pack('!I', len(data)) + data)


def copy_bytes(source, destination, size):
    while size:
        chunk = source.read(min(size, 256 * 1024))
        if not chunk:
            raise ValueError('A file ended during transfer.')
        destination.write(chunk)
        size -= len(chunk)


def pack(path, stream, skip_special=False):
    skipped = 0

    def visit(parent, name, depth):
        nonlocal skipped
        if depth > 256:
            raise ValueError('The transfer exceeds 256 directory levels.')
        mode = os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
        directory = stat.S_ISDIR(mode)
        if not directory and not stat.S_ISREG(mode):
            if skip_special and depth > 0:
                skipped += 1
                return
            raise ValueError('Choose a regular file or directory. The transfer contains a link or special file.')
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(name, flags | (os.O_DIRECTORY if directory else 0), dir_fd=parent)
        try:
            info = os.fstat(fd)
            entry = 'payload' if depth == 0 else checked_name(name)
            if directory:
                header(stream, ['directory', entry])
                for child in sorted(os.listdir(fd)):
                    visit(fd, child, depth + 1)
                header(stream, ['end'])
            else:
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('The source changed during transfer.')
                header(stream, ['file', entry, info.st_size, info.st_mode & 0o777])
                with os.fdopen(fd, 'rb', closefd=False) as src:
                    copy_bytes(src, stream, info.st_size)
        finally:
            os.close(fd)

    visit(None, os.fspath(path), 0)
    stream.flush()
    return skipped


def unpack(stream, parent):
    # parent is a new private directory. Only this function creates its children.
    stack = [Path(parent)]
    first = True
    while True:
        length, = struct.unpack('!I', exact(stream, 4))
        if length > 4096:
            raise ValueError('Invalid transfer record.')
        record = json.loads(exact(stream, length))
        if record == ['end'] and len(stack) > 1:
            stack.pop()
        else:
            if not isinstance(record, list) or len(record) not in (2, 4):
                raise ValueError('Invalid transfer entry.')
            name = checked_name(record[1])
            if first and name != 'payload':
                raise ValueError('Invalid transfer root.')
            target = stack[-1] / name
            if record[0] == 'directory' and len(record) == 2 and len(stack) <= 257:
                target.mkdir(mode=0o700)
                stack.append(target)
            elif record[0] == 'file' and len(record) == 4:
                size, mode = record[2:]
                if type(size) != int or not 0 <= size < 2**63 or type(mode) != int or not 0 <= mode <= 0o777:
                    raise ValueError('Invalid file metadata.')
                with target.open('xb') as dest:
                    copy_bytes(stream, dest, size)
                    os.fchmod(dest.fileno(), mode)
            else:
                raise ValueError('Unexpected transfer entry.')
        first = False
        if len(stack) == 1:
            if stream.read(1):
                raise ValueError('Unexpected data after the transfer.')
            return Path(parent) / 'payload'
