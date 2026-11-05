"""One local utun endpoint for a repeatable split-route test on macOS.

Reply only to TCP SYNs addressed to the test's documentation IPv6 address and
port. The kernel removes the interface and its routes when the control socket
closes. No packets are forwarded to a physical network.
"""
import ctypes
import fcntl
import ipaddress
import select
import socket
import struct
import subprocess
import sys


def checksum(data):
    words = struct.unpack('!' + 'H' * (len(data) // 2), data)
    value = sum(words)
    while value >> 16:
        value = (value & 65535) + (value >> 16)
    return (~value) & 65535


def run(peer, port):
    address = ipaddress.IPv6Address(peer)
    if address not in ipaddress.IPv6Network('2001:db8::/32') or not 1024 <= port <= 65535:
        raise ValueError('Expected a test-only IPv6 address and high port.')
    with socket.socket(32, socket.SOCK_DGRAM, 2) as tun:  # PF_SYSTEM, SYSPROTO_CONTROL
        info = fcntl.ioctl(tun, 0xc0644e03, struct.pack('I96s', 0, b'com.apple.net.utun_control'))
        ctl_id = struct.unpack_from('I', info)[0]
        endpoint = struct.pack('=BBHII5I', 32, 32, 2, ctl_id, 0, 0, 0, 0, 0, 0)
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.connect(tun.fileno(), endpoint, len(endpoint)) < 0:
            raise OSError(ctypes.get_errno(), 'Cannot open a private test tunnel.')
        interface = tun.getsockopt(2, 2, 32).rstrip(b'\0').decode()
        local = str(address - 1)
        subprocess.run(['/sbin/ifconfig', interface, 'inet6', local, 'prefixlen', '128', 'up'], check=True)
        subprocess.run(['/sbin/route', '-n', 'add', '-inet6', '-host', peer, '-interface', interface],
                       check=True, stdout=subprocess.DEVNULL)
        # These two routes cover the public IPv6 range only for sockets bound to
        # this tunnel. Other Mac applications keep their ordinary internet route.
        for network in ('2000::/4', '3000::/4'):
            subprocess.run(['/sbin/route', '-n', 'add', '-inet6', '-net', '-ifscope', interface,
                            network, '-interface', interface], check=True, stdout=subprocess.DEVNULL)
        print(interface, flush=True)
        # EOF on stdin ends the test even when the test driver exits early.
        while True:
            ready, _, _ = select.select([tun, sys.stdin], [], [], 200)
            if tun not in ready or sys.stdin in ready:
                return
            data = tun.recv(65535)
            if len(data) < 64 or data[:4] != struct.pack('!I', socket.AF_INET6):
                continue
            ip = data[4:44]
            tcp = data[44:]
            if ip[6] != 6 or ip[24:40] != address.packed or tcp[13] & 0x12 != 0x02:
                continue
            src, dst, seq = struct.unpack('!HHI', tcp[:8])
            if dst != port:
                continue
            header = struct.pack('!HHIIBBHHH', port, src, 12345, (seq+1) & 0xffffffff, 80, 0x12, 8192, 0, 0)
            addresses = ip[24:40] + ip[8:24]
            check = checksum(addresses + struct.pack('!I3xB', len(header), 6) + header)
            response = header[:16] + struct.pack('!H', check) + header[18:]
            tun.send(data[:4] + struct.pack('!IHBB', 6 << 28, len(response), 6, 64) + addresses + response)


if __name__ == '__main__':
    run(sys.argv[1], int(sys.argv[2]))
