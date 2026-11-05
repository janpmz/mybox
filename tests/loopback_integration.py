"""Test forwarding through the real container service on an existing running box.

Create temporary servers and a temporary Mac listener. Do not restart the box,
install packages, or change saved mappings or firewall rules.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
BACKEND = shutil.which("container")
HELPER = ROOT / ".build/mybox-port-forwarder"
REPORT = ROOT / "test-results/loopback-latest.json"
SERVER = r'''
use strict; use warnings; use IO::Socket::INET; use Fcntl qw(O_WRONLY O_CREAT O_EXCL);
my ($address, $port, $marker) = @ARGV;
my $server = IO::Socket::INET->new(LocalAddr=>$address, LocalPort=>$port, Listen=>20, ReuseAddr=>1) or die $!;
sysopen(my $meta, $marker, O_WRONLY|O_CREAT|O_EXCL, 0600) or die $!;
print $meta "$$ ", $server->sockport, "\n"; close $meta;
$SIG{CHLD} = 'IGNORE';
sub exact {
    my ($client, $count) = @_; my $result = '';
    while (length($result) < $count) {
        my $n = sysread($client, my $data, $count - length($result));
        return undef unless $n; $result .= $data;
    }
    return $result;
}
sub line {
    my ($client) = @_; my $result = '';
    while (length($result) < 8192) {
        my $next = exact($client, 1); return undef unless defined($next);
        $result .= $next; last if $next eq "\n";
    }
    return $result;
}
while (my $client = $server->accept()) {
    my $child = fork(); die $! unless defined($child);
    if ($child) { close $client; next; }
    close $server; $client->autoflush(1);
    my $first = line($client); exit 0 unless defined($first);
    if ($first =~ /^GET /) {
        my $websocket = 0;
        while (my $header = line($client)) {
            last if $header =~ /^\r?\n$/;
            $websocket = 1 if $header =~ /^Upgrade: websocket/i;
        }
        if ($websocket) {
            print $client "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n";
            my $head = exact($client, 2); exit 1 unless defined($head);
            my @head = unpack('CC', $head); my $length = $head[1] & 127;
            exit 1 unless $head[0] == 130 && ($head[1] & 128) && $length < 126;
            my $mask = exact($client, 4); my $data = exact($client, $length);
            exit 1 unless defined($mask) && defined($data);
            for my $index (0..$length-1) { substr($data, $index, 1) ^= substr($mask, $index % 4, 1); }
            print $client pack('CC', 130, $length), $data;
        } else {
            my $body = "mybox-loopback-ok\n";
            print $client "HTTP/1.1 200 OK\r\nContent-Length: ", length($body), "\r\nConnection: close\r\n\r\n", $body;
        }
    } else {
        print $client $first;
        while (my $count = sysread($client, my $data, 65536)) { print $client $data; }
    }
    close $client; exit 0;
}
'''


def run(*args):
    return subprocess.check_output([str(arg) for arg in args], timeout=30)


def records():
    return json.loads(run(BACKEND, "list", "--all", "--format", "json"))


def receive(client, size):
    result = bytearray()
    while len(result) < size:
        data = client.recv(size - len(result))
        assert data, "The connection ended before all bytes arrived."
        result.extend(data)
    return bytes(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("box")
    parser.add_argument("--direct-port", type=int)
    args = parser.parse_args()
    assert re.fullmatch(r"[a-z][a-z0-9-]{0,47}", args.box)
    identifier = "mybox-" + args.box
    before = {item["id"]: item["status"] for item in records()}
    info = json.loads(run(BACKEND, "inspect", identifier))[0]
    assert info["status"]["state"] == "running", "Start the selected box before this test."
    assert info["configuration"]["labels"]["io.github.janpmz.my-container-box.managed"] == "true"
    address = info["status"]["networks"][0]["ipv4Address"].split("/")[0]
    marker = "/tmp/mybox-loopback-" + uuid.uuid4().hex
    report = dict(date=datetime.now(timezone.utc).isoformat(), box=args.box, checks={}, success=False)
    checks = report["checks"]
    helper = None
    server_pid = None
    relay_pids = []

    def guest_relays(inside):
        program = r'''
my $port = shift;
my $suffix = "\0" . $port . "\0";
for my $path (glob('/proc/[0-9]*/cmdline')) {
    my ($pid) = $path =~ m{/proc/([0-9]+)/}; next if $pid == $$;
    open(my $file, '<', $path) or next; local $/; my $command = <$file>; close $file;
    print "$pid\n" if index($command, 'Invalid relay frame') >= 0 && substr($command, -length($suffix)) eq $suffix;
}
'''
        return [int(value) for value in run(BACKEND, "exec", identifier, "/usr/bin/perl", "-e", program, inside).split()]

    def stop_server():
        nonlocal server_pid
        if server_pid is not None:
            run(BACKEND, "exec", identifier, "/bin/kill", "-TERM", str(server_pid))
            server_pid = None
            run(BACKEND, "exec", identifier, "/bin/rm", "--", marker)

    def stop_helper():
        nonlocal helper
        if helper is not None:
            helper.terminate()
            helper.wait(timeout=5)
            helper = None

    try:
        with tempfile.TemporaryDirectory(prefix="mybox-loopback-") as temporary:
            directory = Path(temporary)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                outside = probe.getsockname()[1]

            def start_helper(inside):
                nonlocal helper
                config = dict(version=1, id=identifier, created=info["configuration"]["creationDate"],
                              mappings=[dict(inside=inside, outside=outside)],
                              target=dict(address=address, started=info["status"]["startedDate"]), revision=str(uuid.uuid4()))
                (directory / "ports.json").write_text(json.dumps(config))
                with (directory / "helper.log").open("wb") as log:
                    helper = subprocess.Popen([HELPER, directory, BACKEND], stdin=subprocess.DEVNULL,
                                              stdout=subprocess.DEVNULL, stderr=log, env=dict(os.environ, MYBOX_TRACE="1"))
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    try:
                        status = json.loads((directory / "status.json").read_text())
                        if status["revision"] == config["revision"] and status["active"]:
                            return
                    except (FileNotFoundError, json.JSONDecodeError):
                        pass
                    time.sleep(.05)
                raise AssertionError((directory / "helper.log").read_text())

            for binding in ("127.0.0.1", "0.0.0.0"):
                run(BACKEND, "exec", "--detach", identifier, "/usr/bin/perl", "-e", SERVER, binding, "0", marker)
                for attempt in range(50):
                    result = subprocess.run([BACKEND, "exec", identifier, "/bin/cat", marker], capture_output=True, timeout=10)
                    if result.returncode == 0:
                        server_pid, inside = map(int, result.stdout.split())
                        break
                    time.sleep(.05)
                assert server_pid is not None, "The temporary server did not start."
                start_helper(inside)
                with urllib.request.urlopen(f"http://127.0.0.1:{outside}/", timeout=10) as response:
                    assert response.read() == b"mybox-loopback-ok\n"
                checks[binding + "_http"] = True
                with socket.create_connection(("127.0.0.1", outside), timeout=10) as client:
                    client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n")
                    header = bytearray()
                    while not header.endswith(b"\r\n\r\n"):
                        header.extend(receive(client, 1))
                    assert b"101 Switching Protocols" in header
                    payload, mask = b"\x00\xffhello", b"mask"
                    client.sendall(bytes([130, 128 | len(payload)]) + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(payload)))
                    assert receive(client, 2 + len(payload)) == bytes([130, len(payload)]) + payload
                checks[binding + "_websocket_binary"] = True
                with socket.create_connection(("127.0.0.1", outside), timeout=15) as client:
                    client.sendall(b"echo\n")
                    assert receive(client, 5) == b"echo\n"
                    payload = bytes(range(256)) * 16384
                    errors = []
                    def send():
                        try:
                            client.sendall(payload)
                            client.shutdown(socket.SHUT_WR)
                        except BaseException as error:
                            errors.append(str(error))
                    thread = threading.Thread(target=send)
                    thread.start()
                    assert receive(client, len(payload)) == payload
                    assert client.recv(1) == b""
                    thread.join(timeout=10)
                    assert not thread.is_alive() and not errors, errors
                checks[binding + "_large_binary_and_half_close"] = True
                assert "using box loopback port" in (directory / "helper.log").read_text()
                checks[binding + "_used_guest_relay"] = True
                listing = run("/usr/sbin/lsof", "-nP", f"-iTCP:{outside}", "-sTCP:LISTEN", "-Fn").decode()
                assert [line for line in listing.splitlines() if line.startswith("n")] == [f"n127.0.0.1:{outside}"]
                checks["mac_listener_is_loopback_only"] = True
                with socket.create_connection(("127.0.0.1", outside), timeout=10) as active:
                    active.sendall(b"echo\n")
                    assert receive(active, 5) == b"echo\n"
                    relay_pids = guest_relays(inside)
                    assert relay_pids, "No active guest relay was found."
                    stop_helper()
                    assert active.recv(1) == b""
                    for attempt in range(50):
                        if not guest_relays(inside):
                            relay_pids = []
                            break
                        time.sleep(.1)
                    assert not relay_pids, "Guest relays remained after helper shutdown."
                checks[binding + "_helper_shutdown_cancels_guest_relays"] = True
                stop_server()
                print(f"PASS: {binding} HTTP, WebSocket, binary transfer, and half-close", flush=True)

            if args.direct_port:
                start_helper(args.direct_port)
                with urllib.request.urlopen(f"http://127.0.0.1:{outside}/", timeout=10) as response:
                    assert response.status == 200
                    response.read()
                assert "using box network port" in (directory / "helper.log").read_text()
                checks["existing_network_bound_app_uses_direct_connection"] = True
                stop_helper()
        checks["box_states_and_start_times_unchanged"] = {item["id"]: item["status"] for item in records()} == before
        assert checks["box_states_and_start_times_unchanged"]
        report["success"] = True
    except BaseException as error:
        report["error"] = str(error)
        raise
    finally:
        cleanup = report["cleanup"] = {}
        if relay_pids:
            subprocess.run([BACKEND, "exec", identifier, "/bin/kill", "-TERM", *map(str, relay_pids)], timeout=10)
        for name, action in (("helper", stop_helper), ("temporary_server", stop_server)):
            try:
                action()
                cleanup[name] = True
            except BaseException as error:
                cleanup[name] = str(error)
                report["success"] = False
        REPORT.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
