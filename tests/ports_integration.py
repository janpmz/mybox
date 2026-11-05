"""Test live port changes on a disposable box. Requires the installed firewall v3.

Run as the normal Mac user in a terminal. Administrator authentication uses sudo.
Existing boxes keep their processes and files. No Linux packages are installed.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / ".build/mybox-test"
CONTAINER = shutil.which("container")
REPORT = ROOT / "test-results/ports-latest.json"


def save_report(report):
    REPORT.parent.mkdir(exist_ok=True)
    temporary = REPORT.with_suffix(".pending.json")
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(REPORT)


class Checks(dict):
    def __init__(self, report):
        super().__init__()
        self.report = report

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        save_report(self.report)
        print(f"[{'PASS' if value else 'FAIL'}] {key}", flush=True)

SERVER = r'''
use strict; use warnings; use IO::Socket::INET; use IO::Select;
$SIG{CHLD} = 'IGNORE';
my @servers = map { IO::Socket::INET->new(LocalAddr=>'0.0.0.0', LocalPort=>$_,
    Listen=>20, ReuseAddr=>1) or die "listen: $!" } (8080, 8081);
open my $pid, '>', '/workspace/port-server.pid' or die $!; print $pid "$$\n"; close $pid;
my $select = IO::Select->new(@servers);
while (my @ready = $select->can_read()) {
    for my $server (@ready) {
        my $client = $server->accept() or next;
        my $child = fork();
        if (!defined($child)) { close $client; next; }
        if ($child == 0) {
            close $_ for @servers;
            $client->autoflush(1);
            while (my $line = <$client>) {
                if ($line =~ /^GET /) {
                    while (my $header = <$client>) { last if $header =~ /^\r?\n$/; }
                    my $body = "mybox-live-ports\n";
                    print $client "HTTP/1.1 200 OK\r\nContent-Length: ".length($body)."\r\nConnection: close\r\n\r\n".$body;
                    last;
                }
                print $client $line;
            }
            close $client; exit 0;
        }
        close $client;
    }
}
'''


def run(*args, success=True):
    result = subprocess.run([str(arg) for arg in args], stdout=subprocess.PIPE, stderr=None, timeout=90)
    if (result.returncode == 0) != success:
        raise RuntimeError(f"Unexpected exit {result.returncode}: {args!r}\n{result.stdout.decode(errors='replace')}")
    return result.stdout


def box(*args, **kwargs):
    return run(BOX, *args, **kwargs)


def inspect(identifier):
    return json.loads(run(CONTAINER, "inspect", identifier))[0]


def running():
    return {item["id"]: item["status"].get("startedDate") for item in json.loads(run(CONTAINER, "list", "--format", "json"))}


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def connect_fails(host, port):
    try:
        with socket.create_connection((host, port), timeout=.8):
            return False
    except OSError:
        return True


def echo(client, payload):
    client.sendall(payload)
    received = bytearray()
    while len(received) < len(payload):
        data = client.recv(len(payload) - len(received))
        if not data:
            raise AssertionError("The existing TCP connection closed unexpectedly.")
        received.extend(data)
    assert bytes(received) == payload


def page(port):
    for _ in range(25):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                if response.read() == b"mybox-live-ports\n":
                    return
        except OSError:
            pass
        time.sleep(.1)
    raise AssertionError("The forwarded HTTP server did not answer.")


def start_server(name):
    box("enter", name, "--", "/bin/sh", "-c",
        'nohup /usr/bin/perl -e "$1" >/tmp/mybox-port-server.log 2>&1 </dev/null &', "port-test", SERVER)
    for _ in range(30):
        # Direct exec avoids repeated firewall authentication while waiting.
        result = subprocess.run([CONTAINER, "exec", "mybox-" + name, "/bin/test", "-f", "/workspace/port-server.pid"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if result.returncode == 0:
            return
        time.sleep(.1)
    raise AssertionError("The test server did not start.")


def main():
    if os.geteuid() == 0:
        raise RuntimeError("Run make ports-test as your normal Mac user.")
    name = "ports-check-" + uuid.uuid4().hex[:10]
    identifier = "mybox-" + name
    directory = Path.home() / ".config/mybox/ports" / identifier
    report = dict(date=datetime.now(timezone.utc).isoformat(), box=name, checks={}, success=False)
    checks = report["checks"] = Checks(report)
    save_report(report)
    before = None
    before_rules = before_nat = None
    created = False
    first, second = free_port(), free_port()
    while second == first:
        second = free_port()
    try:
        before = running()
        report["existing_running_before"] = before
        before_rules = run("/usr/bin/sudo", "/sbin/pfctl", "-sr")
        before_nat = run("/usr/bin/sudo", "/sbin/pfctl", "-sn")
        # No ports at creation: this is the user's original situation.
        box("create", name, "--memory", "512m")
        created = True
        start_server(name)
        info = inspect(identifier)
        boot = info["status"]["startedDate"]
        address = info["status"]["networks"][0]["ipv4Address"].split("/")[0]
        pid = run(CONTAINER, "exec", identifier, "/bin/cat", "/workspace/port-server.pid")
        assert info["configuration"]["publishedPorts"] == []
        assert connect_fails(address, 8080)
        checks["unmapped_port_blocked"] = True

        box("ports", "add", name, "--inside-port", "8080", "--outside-port", first)
        page(first)
        assert not connect_fails(address, 8080)
        assert connect_fails(address, 8081)
        listing = run("/usr/sbin/lsof", "-nP", f"-iTCP:{first}", "-sTCP:LISTEN", "-Fn").decode()
        addresses = [line[1:] for line in listing.splitlines() if line.startswith("n")]
        assert addresses and all(value == f"127.0.0.1:{first}" for value in addresses), addresses
        checks["live_add_http_localhost_only_and_selected_firewall_port"] = True

        with socket.create_connection(("127.0.0.1", first), timeout=5) as persistent:
            echo(persistent, b"before\n")
            box("ports", "add", name, "--inside-port", "8080", "--outside-port", second)
            page(second)
            echo(persistent, b"after-add\n")
            checks["add_preserves_existing_tcp_connection"] = True
            with socket.socket() as occupied:
                occupied.bind(("127.0.0.1", 0))
                occupied.listen()
                conflict = occupied.getsockname()[1]
                print(f"[CHECK] Reject occupied port {conflict}. The next error is expected.", flush=True)
                box("ports", "add", name, "--inside-port", "8081", "--outside-port", conflict, success=False)
                assert str(conflict) not in box("ports", "list", name).decode()
                assert connect_fails(address, 8081)
                echo(persistent, b"after-conflict\n")
                checks["conflict_rolls_back_without_losing_existing_connection"] = True
            box("ports", "remove", name, "--outside-port", second)
            assert connect_fails("127.0.0.1", second)
            echo(persistent, b"after-remove\n")
            checks["removing_one_alias_preserves_remaining_mapping"] = True

        box("ports", "remove", name, "--outside-port", first)
        assert connect_fails("127.0.0.1", first)
        assert connect_fails(address, 8080)
        assert inspect(identifier)["status"]["startedDate"] == boot
        assert run(CONTAINER, "exec", identifier, "/bin/cat", "/workspace/port-server.pid") == pid
        checks["live_removal_reblocks_port_without_guest_restart"] = True

        box("ports", "add", name, "--inside-port", "8080", "--outside-port", first)
        status = json.loads((directory / "status.json").read_text())
        os.kill(status["pid"], signal.SIGTERM)
        for _ in range(30):
            if connect_fails("127.0.0.1", first):
                break
            time.sleep(.1)
        box("start", name)
        page(first)
        assert inspect(identifier)["status"]["startedDate"] == boot
        checks["start_repairs_failed_helper_without_guest_restart"] = True

        box("stop", name)
        assert connect_fails("127.0.0.1", first)
        box("ports", "add", name, "--inside-port", "8081", "--outside-port", second)
        assert inspect(identifier)["status"]["state"] == "stopped"
        assert "saved" in box("ports", "list", name).decode()
        box("start", name)
        run(CONTAINER, "exec", identifier, "/bin/rm", "-f", "/workspace/port-server.pid")
        start_server(name)
        page(first)
        page(second)
        checks["stopped_changes_and_restart_restore_mappings"] = True
        report["success"] = True
    except BaseException as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        cleanup = report["cleanup"] = {}

        def cleanup_step(label, action):
            try:
                action()
                cleanup[label] = True
            except Exception as error:
                cleanup[label] = str(error)
                print(f"[FAIL] {label}: {error}", flush=True)
            save_report(report)

        if created:
            cleanup_step("stop_box", lambda: box("stop", name))
            cleanup_step("delete_box", lambda: run(CONTAINER, "delete", identifier))
            cleanup_step("delete_network", lambda: run(CONTAINER, "network", "delete", "mybox-net-" + name))
            if cleanup["delete_box"] is True:
                cleanup_step("remove_port_settings", lambda: shutil.rmtree(directory, ignore_errors=True))
        try:
            if before is not None:
                after = running()
                checks["existing_boxes_kept_running"] = all(after.get(key) == value for key, value in before.items())
            if before_rules is not None and before_nat is not None:
                checks["main_pf_and_nat_rules_preserved"] = (run("/usr/bin/sudo", "/sbin/pfctl", "-sr") == before_rules
                                                             and run("/usr/bin/sudo", "/sbin/pfctl", "-sn") == before_nat)
        except Exception as error:
            cleanup["final_verification"] = str(error)
        report["success"] = report["success"] and all(checks.values()) and all(value is True for value in cleanup.values())
        save_report(report)
        print(json.dumps(report, indent=2), flush=True)
    if not report["success"]:
        raise RuntimeError("Live port checks failed.")


if __name__ == "__main__":
    main()
