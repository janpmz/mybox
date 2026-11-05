"""Reversible pre-start IPv4 PF experiment, with guest capability/IPv6 controls.

Uses only disposable containers, their own network, and temporary host listeners.
Run normally in a Mac terminal; sudo is requested only after baseline checks.
--baseline-only checks the fixture and capability controls without changing PF.
No physical LAN devices or existing boxes are probed or changed.
"""
import argparse
from datetime import date
import fcntl
import http.client
import http.server
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import uuid

from dns import lookup
from network_isolation import (ROOT, PFCTL, PROBE, SERVER, ProbeHandler, command,
                               container, check_internet, rules_for, root_firewall)

# This is the initial workload, not a later setup command. Record its own
# restrictions and attempt host access before running any application fixture.
BOOT = r'''
use strict;
use warnings;
use Socket qw(AF_INET AF_INET6 SOCK_RAW SOCK_DGRAM SOCK_STREAM IPPROTO_RAW IPPROTO_TCP IPPROTO_IP inet_aton sockaddr_in);
use IO::Socket::INET;
my ($host, $port, $udp_port) = @ARGV;
mkdir '/workspace';
open my $log, '>', '/workspace/startup.txt' or die $!;
open my $boot, '<', '/proc/sys/kernel/random/boot_id' or die $!;
print $log 'boot_id=' . <$boot>;
open my $status, '<', '/proc/self/status' or die $!;
while (<$status>) { print $log $_ if /^Cap(?:Eff|Bnd):/; }
my $raw = socket(my $r, AF_INET, SOCK_RAW, IPPROTO_RAW) ? 1 : 0;
my $packet = socket(my $p, 17, SOCK_RAW, 0) ? 1 : 0; # Linux AF_PACKET
my $ipv6 = socket(my $s6, AF_INET6, SOCK_STREAM, IPPROTO_TCP) ? 1 : 0;
print $log "raw_socket=$raw\npacket_socket=$packet\nipv6_socket=$ipv6\n";
socket(my $udp, AF_INET, SOCK_DGRAM, 0) or die $!;
# Linux IP_FREEBIND is unprivileged; it must not permit transmitting an
# out-of-subnet source even though binding it is allowed.
setsockopt($udp, IPPROTO_IP, 15, pack('i', 1)) or die "IP_FREEBIND: $!";
bind($udp, sockaddr_in(0, inet_aton('198.18.0.2'))) or die "freebind: $!";
my $sent = send($udp, 'mybox-freebind-probe', 0, sockaddr_in($udp_port, inet_aton($host)));
my $send_error = "$!";
print $log 'freebind_sent=' . (defined($sent) ? 1 : 0) . "\nfreebind_error=$send_error\n";
my $client = IO::Socket::INET->new(PeerAddr=>$host, PeerPort=>$port, Proto=>'tcp', Timeout=>3);
print $log 'host_connected=' . ($client ? 1 : 0) . "\n";
close $client if $client;
print $log "complete=1\n";
close $log;
sleep 3600 while 1;
'''


def host_http(host, port):
    client = http.client.HTTPConnection(host, port, timeout=3)
    try:
        client.request("GET", "/")
        response = client.getresponse()
        return response.status == 200 and b"MyBox network isolation test" in response.read()
    except (OSError, http.client.HTTPException):
        return False
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", type=Path)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--report", type=Path, default=ROOT / "test-results/prestart-network-latest.json")
    options = parser.parse_args()
    if options.apply:
        root_firewall(options.apply)
        return
    if os.geteuid() == 0:
        raise RuntimeError("Run the main test as your normal Mac user, not with sudo.")

    identifier = uuid.uuid4().hex[:12]
    network = "mybox-prestart-" + identifier
    name = network + "-box"
    alias = "prestart-" + identifier + ".box.test"
    directory = ROOT / ".build" / ("network-test-" + identifier)
    directory.mkdir(mode=0o700, parents=True)
    (directory / "control.lock").touch(mode=0o600)
    report = {"test_date": date.today().isoformat(), "network": network, "box": name,
              "alias": alias, "baseline_only": options.baseline_only, "checks": {}}
    boxes = []
    network_created = False
    firewall_requested = False
    firewall_process = None
    server = None
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Bind only on an already existing host interface. This listener exists
        # before our network/bridge/guest, allowing a real first-instruction test.
        route = command(["/sbin/route", "-n", "get", "default"]).stdout
        interface = re.search(r"interface: ([a-z][a-z0-9]+)", route).group(1)
        host4 = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)\b", command(["/sbin/ifconfig", interface]).stdout).group(1)
        server = http.server.ThreadingHTTPServer((host4, 0), ProbeHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        udp.bind((host4, 0))
        udp.settimeout(0.1)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            outside_port = listener.getsockname()[1]
        report.update(host_listener=host4, outside_port=outside_port)
        container("network", "create", "--label", "io.github.janpmz.my-container-box.network-test=true", network)
        network_created = True
        status = json.loads(container("network", "inspect", network).stdout)[0]["status"]
        gateway = status["ipv4Gateway"]
        if gateway != str(ipaddress.ip_network(status["ipv4Subnet"]).network_address + 1):
            raise RuntimeError("Unexpected gateway allocation")
        sections = re.split(r"(?=^[^\s:]+: flags=)", command(["/sbin/ifconfig"]).stdout, flags=re.M)
        host_interfaces = [section.split(":", 1)[0] for section in sections if re.search(r"\binet [0-9]", section)]
        configuration = {"policy": "prestart-v4", "anchor": "com.apple/000.mybox-net-test-" + identifier,
                         "subnet4": status["ipv4Subnet"], "subnet6": status["ipv6Subnet"],
                         "host_interfaces": host_interfaces, "inside_port": 8080}
        report["configuration"] = configuration
        (directory / "server.pl").write_text(SERVER)

        def create(box):
            container("create", "--name", box, "--network", network + ",alias=" + alias,
                      "--memory", "256m", "--cpus", "1", "--dns", "1.1.1.1", "--dns", "1.0.0.1",
                      "--cap-drop", "NET_RAW", "--cap-drop", "NET_ADMIN", "--kernel-arg", "ipv6.disable=1",
                      "--publish", f"127.0.0.1:{outside_port}:8080", "--entrypoint", "/usr/bin/perl",
                      "debian:trixie-slim", "-e", BOOT, host4, str(server.server_port), str(udp.getsockname()[1]))
            boxes.append(box)

        def probe(box, host, port, phase_timeout=3):
            result = container("exec", box, "/usr/bin/perl", "-e", PROBE, host, str(port), "4", "1",
                               str(phase_timeout), check=False, timeout=phase_timeout * 3 + 5)
            return {"connected": result.returncode == 0, "output": result.stdout.strip(), "error": result.stderr.strip()}

        def observe(box, stage, blocked):
            deadline = time.monotonic() + 15
            boot_id = container("exec", box, "/bin/cat", "/proc/sys/kernel/random/boot_id").stdout.strip()
            while True:
                startup = container("exec", box, "/bin/cat", "/workspace/startup.txt", check=False).stdout
                if "complete=1" in startup and "boot_id=" + boot_id in startup:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("First workload did not finish its checks")
                time.sleep(0.2)
            bnd = int(re.search(r"CapBnd:\s*([0-9a-f]+)", startup).group(1), 16)
            controls = all(key + "=0" in startup for key in ["raw_socket", "packet_socket", "ipv6_socket", "freebind_sent"])
            controls = controls and bnd & ((1 << 12) | (1 << 13)) == 0
            try:
                datagram, source = udp.recvfrom(2048)
                report[stage + "_unexpected_datagram"] = {"data": datagram.decode(errors="replace"), "source": source}
                controls = False
            except socket.timeout:
                pass
            container("cp", str(directory / "server.pl"), box + ":/workspace/server.pl")
            for port in [8080, 8081]:
                container("exec", "--detach", box, "/usr/bin/perl", "/workspace/server.pl", str(port))
            guest = json.loads(container("inspect", box).stdout)[0]["status"]["networks"][0]
            guest4 = guest["ipv4Address"].split("/")[0]
            # Verify both servers exist even when the host cannot reach one.
            servers_work = all(probe(box, "127.0.0.1", port)["connected"] for port in [8080, 8081])
            internet = check_internet(lambda host, port, **kw: probe(box, host, port, **kw))
            observed = {"startup": startup, "guest_ipv4": guest4, "A": lookup(alias), "AAAA": lookup(alias, "AAAA"),
                        "internet": internet, "direct_selected": host_http(alias, 8080),
                        "direct_unpublished": host_http(guest4, 8081), "localhost": host_http("127.0.0.1", outside_port),
                        "host_after_start": probe(box, host4, server.server_port)}
            report[stage] = observed
            checks = {"controls_present_in_first_workload": controls,
                      "first_workload_host_access": ("host_connected=" + ("0" if blocked else "1")) in startup,
                      "host_access_after_start": observed["host_after_start"]["connected"] != blocked,
                      "internet": internet["connected"], "servers_running": servers_work,
                      "dns_current": observed["A"] == [guest4], "direct_selected": observed["direct_selected"],
                      "unpublished_policy": observed["direct_unpublished"] != blocked, "localhost": observed["localhost"]}
            report["checks"][stage] = checks
            print(stage + ": " + json.dumps(checks), flush=True)
            if not all(checks.values()):
                raise RuntimeError(stage + " failed: " + repr([key for key, value in checks.items() if not value]))

        create(name + "-baseline")
        container("start", boxes[-1])
        observe(boxes[-1], "baseline", False)
        container("stop", boxes[-1])
        container("delete", boxes[-1])
        boxes.pop()
        if options.baseline_only:
            report["success"] = True
            return

        create(name)  # First boot of a new guest must occur only after PF is active.
        if re.search(r"\binet " + re.escape(gateway) + r"\s", command(["/sbin/ifconfig"]).stdout):
            raise RuntimeError("Guest bridge still exists before pre-start rules")
        report["bridge_absent_before_rules"] = True
        (directory / "configuration.json").write_text(json.dumps(configuration, indent=2) + "\n")
        (directory / "candidate.pf").write_text(rules_for(configuration))
        command([PFCTL, "-a", configuration["anchor"], "-nf", str(directory / "candidate.pf")])
        firewall_requested = True
        privileged = ["/usr/bin/sudo", "/usr/bin/python3", str(Path(__file__).resolve()), "--apply", str(directory)]
        if sys.stdin.isatty():
            print("Baseline passed. Authenticate for temporary rules before the new VM starts.", flush=True)
            firewall_process = subprocess.Popen(privileged)
        else:
            print("Authenticate in a Mac terminal within five minutes:\n" + shlex.join(privileged), flush=True)
        deadline = time.monotonic() + 300
        while not (directory / "active").exists():
            if (directory / "firewall-error").exists():
                raise RuntimeError((directory / "firewall-error").read_text())
            if firewall_process is not None and firewall_process.poll() is not None:
                raise RuntimeError("Administrator stage failed")
            if time.monotonic() >= deadline:
                raise TimeoutError("Administrator authentication was not completed")
            time.sleep(0.25)
        report["rules_active_before_first_start"] = True
        for stage in ["first_start", "restart"]:
            container("start", name)
            observe(name, stage, True)
            container("stop", name)
        report["success"] = True
    except BaseException as error:
        report["success"] = False
        report["error"] = str(error)
        raise
    finally:
        # Keep rules installed until the guest has stopped, including on failure.
        for box in boxes:
            container("stop", box, check=False)
        if firewall_requested:
            (directory / "done").touch(exist_ok=True)
        with (directory / "control.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            report["cleanup"] = {}
            for box in boxes:
                report["cleanup"][box] = container("delete", box, check=False).returncode == 0
            if network_created:
                report["cleanup"][network] = container("network", "delete", network, check=False).returncode == 0
        if firewall_process is not None:
            firewall_process.wait(timeout=10)
        if (directory / "firewall-result.json").exists():
            pf = json.loads((directory / "firewall-result.json").read_text())
            report["firewall"] = pf
            report["success"] = report.get("success", False) and not pf.get("error") and not pf["cleanup_errors"] and not pf["remaining_rules"].strip() and pf["main_rules_unchanged"] and pf["main_nat_unchanged"]
        if server:
            server.shutdown()
            server.server_close()
        udp.close()
        report["success"] = report.get("success", False) and all(report["cleanup"].values())
        options.report.write_text(json.dumps(report, indent=2) + "\n")
        print("Results: " + str(options.report), flush=True)
    if not report["success"]:
        raise RuntimeError("Cleanup verification failed")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, TimeoutError) as error:
        print("Pre-start test: " + str(error), file=sys.stderr)
        sys.exit(1)
