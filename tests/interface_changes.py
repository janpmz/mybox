"""Test automatic PF refresh while a disposable box keeps running.

Run from a Mac terminal. This installs the watcher and uses a temporary bridge.
No physical interface or main PF rule is changed. The test temporarily pauses
the watcher to create one connection under the old policy, then resumes it.
It refuses to run alongside other running MyBox boxes.
"""
import json
import os
from pathlib import Path
import random
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid

from network_isolation import ROOT, PROBE, SERVER, check_internet
from prestart_network import host_http

BOX = str(ROOT / ".build/mybox")
HELPER = "/Library/PrivilegedHelperTools/mybox-firewall-manager"
ANCHOR = "com.apple/000.mybox"


def run(*args, check=True, timeout=90):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"Command failed: {args!r}\n{result.stdout}{result.stderr}")
    return result


def sudo(*args, check=True):
    return run("/usr/bin/sudo", "-n", *args, check=check)


def box(*args, check=True):
    # MyBox authenticates through /dev/tty. Guest commands must not inherit the terminal input descriptor.
    result = subprocess.run([BOX, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True,
                            timeout=None if args == ("setup-network",) else 180)
    if check and result.returncode:
        raise RuntimeError("MyBox failed: " + repr(args) + "\n" + result.stdout)
    return result


def wait_for(check, timeout=15, description="network-change check"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise RuntimeError("Timed out while waiting for " + description + ".")


def wait_for_interface(name, present, timeout=15):
    latest = None

    def refreshed():
        nonlocal latest
        latest = json.loads(sudo("/bin/cat", "/var/db/mybox/snapshot.json").stdout)
        names = {item["name"] for item in latest["interfaces"]}
        return latest if not latest["blocked"] and (name in names) == present else None

    action = "addition" if present else "removal"
    try:
        return wait_for(refreshed, timeout=timeout, description=f"interface {action}: {name}")
    except RuntimeError as error:
        raise RuntimeError(f"{error} Last snapshot: {json.dumps(latest)}") from error


def prepare_terminal_output():
    # Repair terminal flags left by a previous run. Pipe output retains its caller's settings.
    for stream in (sys.stdout, sys.stderr):
        if stream.isatty():
            os.set_blocking(stream.fileno(), True)


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            while data := self.request.recv(1024):
                self.request.sendall(data)
        except OSError:
            pass


class EchoServer(socketserver.ThreadingTCPServer):
    daemon_threads = True


PERSISTENT = r'''
use strict;
use warnings;
use IO::Socket::INET;
my ($host, $port, $path) = @ARGV;
open my $log, '>', $path or die $!;
$log->autoflush(1);
my $socket = IO::Socket::INET->new(PeerAddr=>$host, PeerPort=>$port, Proto=>'tcp', Timeout=>3)
    or die "connect: $!";
$socket->autoflush(1);
print $log "connected\n";
for (1..600) {
    my $ok = eval {
        local $SIG{ALRM} = sub { die "timeout" };
        local $SIG{PIPE} = sub { die "closed" };
        alarm 2;
        print $socket "ping\n" or die "write";
        my $reply = <$socket>;
        die "read" unless defined($reply) && $reply eq "ping\n";
        alarm 0;
        1;
    };
    alarm 0;
    if (!$ok) { print $log "blocked\n"; exit 0; }
    print $log "reply\n";
    select undef, undef, undef, 0.05;
}
die "connection survived the test deadline";
'''


def main():
    prepare_terminal_output()
    if os.geteuid() == 0:
        raise RuntimeError("Run this test as your normal Mac user.")
    existing = json.loads(run("container", "list", "--format", "json").stdout)
    if any(item["configuration"].get("labels", {}).get("com.mybox.managed") == "true" for item in existing):
        raise RuntimeError("Stop other MyBox boxes before this test. It briefly pauses automatic firewall refresh.")
    name = "interface-check-" + uuid.uuid4().hex[:8]
    identifier = "mybox-" + name
    report = {"box_name": name, "checks": {}, "cleanup": {}, "success": False,
              "zero_gap_guarantee": False, "controlled_watcher_pause": True}
    checks = report["checks"]
    created = False
    bridge = None
    paused_pid = None
    server = None
    report_path = ROOT / "test-results/interface-changes-latest.json"
    try:
        print("Installing the firewall watcher. Administrator authentication may be required.", flush=True)
        box("setup-network")
        wait_for(lambda: sudo(HELPER, "status", check=False).returncode == 0)
        before_main = sudo("/sbin/pfctl", "-sr").stdout
        before_nat = sudo("/sbin/pfctl", "-sn").stdout
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        # Record ownership before creation so partial startup failures are cleaned up too.
        created = True
        box("create", name, "--inside-port", "8080", "--outside-port", str(port), "--memory", "256m", "--cpus", "1")
        boot_before = run("container", "exec", identifier, "/bin/cat", "/proc/sys/kernel/random/boot_id").stdout
        run("container", "exec", "--detach", identifier, "/usr/bin/perl", "-e", SERVER, "8080")
        wait_for(lambda: host_http("127.0.0.1", port))

        print("Creating a controlled interface change and an existing connection.", flush=True)
        status = json.loads(sudo(HELPER, "status").stdout)
        paused_pid = status["pid"]
        sudo("/bin/kill", "-STOP", str(paused_pid))
        bridge = sudo("/sbin/ifconfig", "bridge", "create").stdout.strip()
        if not re.fullmatch(r"bridge[0-9]+", bridge):
            raise RuntimeError("Unexpected test bridge name: " + repr(bridge))
        report["test_interface"] = bridge
        # Use a locally assigned public-range /32 to exercise the dynamic host rule.
        # Private/documentation ranges would be blocked even by the old static policy.
        # Probe this address only while it belongs to our temporary local interface.
        address = f"11.255.{random.randrange(1, 255)}.253"
        if address in run("/sbin/ifconfig").stdout:
            raise RuntimeError("The test address is already in use on this Mac.")
        sudo("/sbin/ifconfig", bridge, "inet", address, "netmask", "255.255.255.255", "up")
        if address not in run("/sbin/ifconfig", bridge).stdout:
            raise RuntimeError("The test address was not assigned locally.")
        server = EchoServer((address, 0), Echo)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log = "/tmp/mybox-interface-probe.txt"
        run("container", "exec", "--detach", identifier, "/usr/bin/perl", "-e", PERSISTENT,
            address, str(server.server_address[1]), log)

        def probe_log():
            return run("container", "exec", identifier, "/bin/cat", log, check=False).stdout

        wait_for(lambda: "reply\n" in probe_log())
        checks["old_policy_connection_established"] = True
        start = time.monotonic()
        sudo("/bin/kill", "-CONT", str(paused_pid))
        paused_pid = None

        wait_for_interface(bridge, present=True)
        report["resume_to_completed_refresh_seconds"] = round(time.monotonic() - start, 4)
        checks["automatic_refresh_without_box_command"] = True
        wait_for(lambda: "blocked\n" in probe_log())
        checks["existing_connection_closed"] = True
        output = run("container", "exec", identifier, "/usr/bin/perl", "-e", PROBE,
                     address, str(server.server_address[1]), "4", "0", "2", check=False)
        checks["new_host_connection_blocked"] = output.returncode != 0
        checks["command_after_change_succeeds"] = box("enter", name, "--", "/bin/true").returncode == 0
        checks["box_did_not_restart"] = boot_before == run("container", "exec", identifier, "/bin/cat", "/proc/sys/kernel/random/boot_id").stdout
        checks["published_port_still_works"] = host_http("127.0.0.1", port)

        def internet_probe(host, target_port, phase_timeout=4):
            result = run("container", "exec", identifier, "/usr/bin/perl", "-e", PROBE,
                         host, str(target_port), "4", "1", str(phase_timeout), check=False)
            return {"connected": result.returncode == 0, "output": result.stdout, "error": result.stderr}

        report["internet"] = check_internet(internet_probe)
        checks["internet_and_dns_work"] = report["internet"]["connected"]
        report["watcher_status"] = json.loads(sudo(HELPER, "status").stdout)

        # Both probes exited before removing the local address. No packet is sent to its Internet owner.
        server.shutdown()
        server.server_close()
        server = None
        removed_bridge = bridge
        removal_start = time.monotonic()
        sudo("/sbin/ifconfig", bridge, "destroy")
        report["cleanup"]["bridge_deleted"] = True
        bridge = None
        wait_for_interface(removed_bridge, present=False)
        report["interface_removal_to_refresh_seconds"] = round(time.monotonic() - removal_start, 4)
        checks["interface_removal_refreshed"] = True
        checks["command_after_removal_succeeds"] = box("enter", name, "--", "/bin/true").returncode == 0
        checks["main_pf_rules_preserved"] = before_main == sudo("/sbin/pfctl", "-sr").stdout
        checks["main_nat_rules_preserved"] = before_nat == sudo("/sbin/pfctl", "-sn").stdout
        report["success"] = all(checks.values())
        if not report["success"]:
            raise RuntimeError("An interface-change check failed.")
    except BaseException as error:
        report["error"] = repr(error)
        raise
    finally:
        if paused_pid is not None:
            report["cleanup"]["watcher_resumed"] = sudo("/bin/kill", "-CONT", str(paused_pid), check=False).returncode == 0
        if created:
            # Stop the box before removing its probe destination, including failure paths.
            stopped = box("stop", name, check=False).returncode == 0
            if not stopped:
                stopped = run("container", "stop", identifier, check=False).returncode == 0
            report["cleanup"]["box_stopped"] = stopped
            report["cleanup"]["box_deleted"] = run("container", "delete", identifier, check=False).returncode == 0
            report["cleanup"]["network_deleted"] = run("container", "network", "delete", "mybox-net-" + name, check=False).returncode == 0
        if server is not None:
            server.shutdown()
            server.server_close()
        if bridge is not None:
            # Preserve the local route if a failed stop could leave the probe running.
            if not created or report["cleanup"].get("box_stopped"):
                report["cleanup"]["bridge_deleted"] = sudo("/sbin/ifconfig", bridge, "destroy", check=False).returncode == 0
            else:
                report["cleanup"]["bridge_deleted"] = False
        report["success"] = report["success"] and all(report["cleanup"].values())
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        print("Results: " + str(report_path), flush=True)
        print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
