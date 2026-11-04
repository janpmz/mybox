"""Reversible macOS PF experiment. Uses only installed Python, Perl, and container.

Run as the normal macOS user. In a Mac terminal the test requests sudo
authentication directly; otherwise it prints a command for a separate terminal.
That stage applies only this test's PF anchor, waits for the checks, and removes
its own rules and PF enable reference automatically.
This is an experiment, not the production MyBox network implementation.
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
import shutil
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
CONTAINER = shutil.which("container")
PFCTL = "/sbin/pfctl"

PRIVATE_V4 = ["0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
              "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
              "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
              "224.0.0.0/4", "240.0.0.0/4"]

PROBE = r'''use strict;
use warnings;
use Socket qw(AF_INET AF_INET6 SOCK_STREAM IPPROTO_TCP inet_pton inet_ntoa pack_sockaddr_in6);
use IO::Socket::INET;
my ($host, $port, $family, $http, $timeout) = @ARGV;
$timeout //= 4;
my $phase = 'TCP connection';
eval {
    local $SIG{ALRM} = sub { die "timeout\n" };
    my $socket;
    if ($family eq '6') {
        alarm $timeout;
        socket($socket, AF_INET6, SOCK_STREAM, IPPROTO_TCP) or die "socket: $!\n";
        connect($socket, pack_sockaddr_in6($port, inet_pton(AF_INET6, $host))) or die "connect: $!\n";
    } else {
        my $address = $host;
        if ($host !~ /^\d+\.\d+\.\d+\.\d+$/) {
            $phase = 'DNS lookup';
            alarm $timeout;
            my @addresses = gethostbyname($host);
            die "no IPv4 address\n" unless @addresses && defined $addresses[4];
            $address = inet_ntoa($addresses[4]);
            print "Resolved IPv4: $address\n";
        }
        $phase = 'TCP connection';
        alarm $timeout;
        $socket = IO::Socket::INET->new(PeerAddr => $address, PeerPort => $port,
                                       Proto => 'tcp', Timeout => $timeout) or die "connect: $!\n";
    }
    if ($http) {
        $phase = 'HTTP response';
        alarm $timeout;
        $socket->autoflush(1);
        print $socket "GET / HTTP/1.1\r\nHost: $host\r\nConnection: close\r\n\r\n";
        my $line = <$socket>;
        die "no HTTP response\n" unless defined $line;
        die "invalid HTTP response\n" unless $line =~ /^HTTP\/1\.[01] [1-5][0-9]{2}(?: |\r?\n)/;
        print $line;
    } else {
        print "connected\n";
    }
    close $socket;
    alarm 0;
};
if ($@) { print STDERR "$phase: $@"; exit 1; }
'''

SERVER = r'''use strict;
use warnings;
use IO::Socket::INET;
my $port = $ARGV[0] // 8080;
my $listener = IO::Socket::INET->new(LocalAddr => '0.0.0.0', LocalPort => $port,
    Listen => 10, ReuseAddr => 1) or die $!;
while (my $client = $listener->accept()) {
    eval {
        local $SIG{ALRM} = sub { die "timeout" };
        alarm 3;
        while (my $line = <$client>) { last if $line =~ /^\r?\n$/; }
        my $body = "MyBox network isolation test\n";
        print $client "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
            . length($body) . "\r\nConnection: close\r\n\r\n" . $body;
        alarm 0;
    };
    alarm 0;
    close $client;
}
'''


def command(arguments, check=True, timeout=40):
    # Test commands need no input. The separate sudo stage inherits the user's
    # terminal explicitly; backend commands should not take ownership of it.
    result = subprocess.run(arguments, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{arguments!r}: {result.stderr.strip()} {result.stdout.strip()}")
    return result


def container(*arguments, **kwargs):
    return command([CONTAINER, *arguments], **kwargs)


def check_internet(probe, attempts=2, retry_delay=0.5):
    """Retry only the external check; preserve every attempt and fail if none pass."""
    history = []
    for attempt in range(1, attempts + 1):
        print(f"Checking IPv4 Internet/DNS (attempt {attempt}/{attempts})...", flush=True)
        result = probe("example.com", 80, phase_timeout=12)
        history.append(result)
        if result["connected"]:
            return dict(result, attempts=history)
        print("  " + result["error"], flush=True)
        if attempt < attempts:
            time.sleep(retry_delay)
    return dict(history[-1], attempts=history)


def require_baseline(results):
    failures = []
    for name, result in results.items():
        if isinstance(result, dict) and not result["connected"]:
            failures.append(name + ": " + result["error"])
        elif result is False:
            failures.append(name)
    if failures:
        raise RuntimeError("Baseline connectivity failed (" + "; ".join(failures) + "). No firewall rules were applied.")


def interfaces_for_gateway(gateway):
    deadline = time.monotonic() + 12
    last_section = ""
    while time.monotonic() < deadline:
        text = command(["/sbin/ifconfig"]).stdout
        sections = re.split(r"(?=^[^\s:]+: flags=)", text, flags=re.M)
        for section in sections:
            if re.search(r"\binet " + re.escape(gateway) + r"\s", section):
                last_section = section
                interface = section.split(":", 1)[0]
                if not re.fullmatch(r"bridge[0-9]+", interface):
                    raise RuntimeError("The test network did not use a dedicated bridge.")
                members = re.findall(r"\bmember: (vmenet[0-9]+)\b", section)
                addresses6 = re.findall(r"\binet6 ([0-9a-f:]+)(?:%[^\s]+)? prefixlen", section)
                ula = next((address for address in addresses6 if ipaddress.ip_address(address) in ipaddress.ip_network("fc00::/7")), None)
                if members and ula:
                    try:
                        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as readiness:
                            readiness.bind((ula, 0))
                        return [interface, *members], ula
                    except OSError:
                        pass  # macOS can list an IPv6 address before duplicate-address detection completes.
        time.sleep(0.2)
    raise RuntimeError("Could not identify the test bridge's VM interfaces and IPv6 address:\n" + last_section)


def rules_for(configuration):
    policy = configuration.get("policy", "interface")
    if policy not in ("interface", "prestart-v4"):
        raise ValueError("Unknown test policy")
    if policy == "interface":
        interfaces = configuration["interfaces"]
        if not interfaces or any(not re.fullmatch(r"(?:bridge|vmenet)[0-9]+", value) for value in interfaces):
            raise ValueError("Invalid network interfaces")
        bridges = [interface for interface in interfaces if interface.startswith("bridge")]
        if len(bridges) != 1:
            raise ValueError("Expected exactly one dedicated bridge")
    host_interfaces = configuration["host_interfaces"]
    if not host_interfaces or any(not re.fullmatch(r"[a-z][a-z0-9]{0,15}", value) for value in host_interfaces):
        raise ValueError("Invalid host address interfaces")
    network4 = ipaddress.ip_network(configuration["subnet4"])
    network6 = ipaddress.ip_network(configuration["subnet6"])
    if network4.version != 4 or network6.version != 6 or not network4.is_private or not network6.is_private:
        raise ValueError("Expected private test network subnets")
    port = configuration["inside_port"]
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Invalid port")
    host_addresses = "{ " + ", ".join("(" + interface + ")" for interface in host_interfaces) + " }"
    connected_networks = "{ " + ", ".join("(" + interface + ":network)" for interface in host_interfaces) + " }"
    if policy == "prestart-v4":
        # Experimental: requires NET_RAW/NET_ADMIN drops and IPv6 disabled at
        # boot. Source-range rules alone are bypassable (source_spoof.py).
        private_ranges = [ipaddress.ip_network(value) for value in ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]]
        if network4.prefixlen != 24 or not any(network4.subnet_of(value) for value in private_ranges):
            raise ValueError("Pre-start experiment requires a private /24")
        if network6.prefixlen != 64 or not network6.subnet_of(ipaddress.ip_network("fc00::/7")):
            raise ValueError("Pre-start experiment requires a ULA /64")
        gateway = network4.network_address + 1
        return "\n".join([
            "# Experimental pre-start source-range policy; guest restrictions required.",
            f'pass out quick inet proto tcp from {gateway} to {network4} port {port} flags S/SA keep state label "mybox_selected_port"',
            f'block drop out quick inet to {network4} label "mybox_unpublished"',
            f'block drop in quick inet from {network4} to {host_addresses} label "mybox_host"',
            f'block drop in quick inet from {network4} to {connected_networks} label "mybox_connected_network"',
            f'block drop in quick inet from {network4} to {{ ' + ", ".join(PRIVATE_V4) + ' } label "mybox_private"',
            f'pass in quick inet from {network4} to any keep state label "mybox_internet"',
            f'block drop in quick inet6 from {network6} label "mybox_ipv6"',
            f'block drop out quick inet6 to {network6} label "mybox_ipv6"',
            "",
        ])
    bridge = bridges[0]
    return "\n".join([
        "# Incoming restrictions are confined to the dedicated test bridge.",
        "# Host-to-box restrictions are confined to this test network's IPv4 subnet.",
        f'pass out quick inet proto tcp from {host_addresses} to {network4} port {port} flags S/SA keep state label "mybox_selected_port"',
        f'block drop out quick inet from {host_addresses} to {network4} label "mybox_unpublished"',
        f'block drop in quick on {bridge} inet to {host_addresses} label "mybox_host"',
        f'block drop in quick on {bridge} inet to {connected_networks} label "mybox_connected_network"',
        f'block drop in quick on {bridge} inet to {{ ' + ", ".join(PRIVATE_V4) + ' } label "mybox_private"',
        f'block drop in quick on {bridge} inet6 all label "mybox_ipv6"',
        "",
    ])


def root_firewall(directory):
    """Serialize applying/cleanup, and refuse abandoned or expired test runs."""
    if os.geteuid() != 0:
        raise RuntimeError("This mode requires sudo in a Mac terminal.")
    directory = Path(directory).resolve(strict=True)
    with (directory / "control.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (directory / "done").exists() or time.time() - (directory / "configuration.json").stat().st_mtime > 300:
            raise RuntimeError("This test has ended. Run make network-test as your normal user to start a fresh test. No rules were changed.")
        root_firewall_locked(directory)


def root_firewall_locked(directory):
    """Temporary root operation; no daemon, installation, or main-rules reload."""
    configuration = json.loads((directory / "configuration.json").read_text())
    anchor = configuration["anchor"]
    if not re.fullmatch(r"com\.apple/000\.mybox-net-test-[a-f0-9]{12}", anchor):
        raise ValueError("Invalid test anchor")
    rules = rules_for(configuration)
    rule_path = directory / "active.pf"
    with rule_path.open("x") as output:
        output.write(rules)
    rule_path.chmod(0o644)
    token = None
    installed = False
    result = {}
    before_rules = command([PFCTL, "-sr"]).stdout
    before_nat = command([PFCTL, "-sn"]).stdout
    try:
        if 'anchor "com.apple/*"' not in before_rules:
            raise RuntimeError("The active main rules do not contain the expected com.apple anchor. No rules were changed.")
        command([PFCTL, "-a", anchor, "-nf", str(rule_path)])
        enabled = command([PFCTL, "-E"])
        match = re.search(r"Token\s*:\s*(\d+)", enabled.stdout + enabled.stderr)
        if not match:
            raise RuntimeError("PF did not return an enable-reference token.")
        token = match[1]
        command([PFCTL, "-a", anchor, "-f", str(rule_path)])
        installed = True
        for subnet, any_source in [(configuration["subnet4"], "0.0.0.0/0"), (configuration["subnet6"], "::/0")]:
            command([PFCTL, "-k", subnet])
            command([PFCTL, "-k", any_source, "-k", subnet])
        (directory / "active").touch(exist_ok=False)
        print("Temporary MyBox firewall is active. Waiting for the test; cleanup is automatic.", flush=True)
        deadline = time.monotonic() + 180
        while not (directory / "done").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Test timed out; removing temporary rules.")
            time.sleep(0.25)
        result["counters"] = command([PFCTL, "-a", anchor, "-vvsr"]).stdout
    except BaseException as error:
        result["error"] = str(error)
        (directory / "firewall-error").write_text(str(error))
        raise
    finally:
        cleanup_errors = []
        if installed:
            flushed = command([PFCTL, "-a", anchor, "-F", "rules"], check=False)
            if flushed.returncode:
                cleanup_errors.append(flushed.stderr.strip())
        if token:
            released = command([PFCTL, "-X", token], check=False)
            if released.returncode:
                cleanup_errors.append(released.stderr.strip())
        result["remaining_rules"] = command([PFCTL, "-a", anchor, "-sr"]).stdout
        result["main_rules_unchanged"] = before_rules == command([PFCTL, "-sr"]).stdout
        result["main_nat_unchanged"] = before_nat == command([PFCTL, "-sn"]).stdout
        result["cleanup_errors"] = cleanup_errors
        (directory / "firewall-result.json").write_text(json.dumps(result, indent=2) + "\n")
        (directory / "cleaned").touch(exist_ok=False)
        if cleanup_errors or result["remaining_rules"].strip():
            print("Firewall cleanup needs attention. See firewall-result.json in the test directory.", flush=True)
        else:
            print("Temporary MyBox firewall removed. Main firewall rules were preserved.", flush=True)


class ProbeHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"MyBox host probe\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *arguments):
        pass


class IPv6HTTPServer(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", type=Path, help="Temporary PF stage, run with sudo in a separate Mac terminal")
    parser.add_argument("--baseline-only", action="store_true", help="Check connectivity and clean up without requesting PF changes")
    parser.add_argument("--report", type=Path, default=ROOT / "test-results" / "network-isolation-latest.json")
    options = parser.parse_args()
    if options.apply:
        root_firewall(options.apply)
        return
    if os.geteuid() == 0:
        raise RuntimeError("Start the main test as your normal macOS user, not with sudo.")
    if not CONTAINER:
        raise RuntimeError("Apple's container CLI is required.")
    identifier = uuid.uuid4().hex[:12]
    name = "mybox-net-" + identifier
    network = name + "-network"
    directory = ROOT / ".build" / ("network-test-" + identifier)
    directory.mkdir(mode=0o700, parents=True)
    (directory / "control.lock").touch(mode=0o600)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        outside_port = probe.getsockname()[1]
    report = {"test_date": date.today().isoformat(), "box": name, "network": network,
              "outside_port": outside_port, "checks": {}, "scope": "Temporary interface-scoped PF test; IPv4 Internet, IPv6 blocked"}
    report["baseline_only"] = options.baseline_only
    network_created = False
    box_created = False
    firewall_requested = False
    firewall_process = None
    servers = []
    try:
        container("network", "create", "--label", "com.mybox.network-test=true", network)
        network_created = True
        network_info = json.loads(container("network", "inspect", network).stdout)[0]
        container("create", "--name", name, "--network", network, "--memory", "256m", "--cpus", "1",
                  "--dns", "1.1.1.1", "--dns", "1.0.0.1", "--cap-drop", "NET_ADMIN", "--cap-drop", "NET_RAW",
                  "--publish", f"127.0.0.1:{outside_port}:8080", "--entrypoint", "/bin/sh",
                  "debian:trixie-slim", "-c", "mkdir -p /workspace && exec sleep infinity")
        box_created = True
        container("start", name)
        configuration = {"anchor": "com.apple/000.mybox-net-test-" + identifier,
                         "subnet4": network_info["status"]["ipv4Subnet"],
                         "subnet6": network_info["status"]["ipv6Subnet"], "inside_port": 8080}
        gateway = network_info["status"]["ipv4Gateway"]
        configuration["interfaces"], host6 = interfaces_for_gateway(gateway)
        interface_sections = re.split(r"(?=^[^\s:]+: flags=)", command(["/sbin/ifconfig"]).stdout, flags=re.M)
        configuration["host_interfaces"] = [section.split(":", 1)[0] for section in interface_sections if re.search(r"\binet [0-9]", section)]
        status = json.loads(container("inspect", name).stdout)[0]["status"]
        guest4 = status["networks"][0]["ipv4Address"].split("/", 1)[0]
        report["configuration"] = configuration
        (directory / "configuration.json").write_text(json.dumps(configuration, indent=2) + "\n")
        (directory / "candidate.pf").write_text(rules_for(configuration))
        command([PFCTL, "-a", configuration["anchor"], "-nf", str(directory / "candidate.pf")])
        for filename, content in [("probe.pl", PROBE), ("server.pl", SERVER)]:
            (directory / filename).write_text(content)
            container("cp", str(directory / filename), name + ":/workspace/" + filename)
        container("exec", "--detach", name, "/usr/bin/perl", "/workspace/server.pl")
        container("exec", "--detach", name, "/usr/bin/perl", "/workspace/server.pl", "8081")
        for server_type, address in [(http.server.ThreadingHTTPServer, (gateway, 0)), (IPv6HTTPServer, (host6, 0))]:
            server = server_type(address, ProbeHandler)
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()

        def probe(host, port, family="4", http=True, phase_timeout=4):
            started = time.monotonic()
            result = container("exec", name, "/usr/bin/perl", "/workspace/probe.pl", host, str(port), family,
                               "1" if http else "0", str(phase_timeout), check=False, timeout=phase_timeout * 3 + 5)
            return {"connected": result.returncode == 0, "output": result.stdout.strip(), "error": result.stderr.strip(),
                    "elapsed_seconds": round(time.monotonic() - started, 3)}

        def host_http(host, port):
            connection = http.client.HTTPConnection(host, port, timeout=4)
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                connected = response.status == 200 and b"MyBox network isolation test" in response.read()
                return {"connected": connected, "error": "" if connected else "Unexpected HTTP response"}
            except (OSError, http.client.HTTPException) as error:
                return {"connected": False, "error": str(error)}
            finally:
                connection.close()

        def localhost():
            return host_http("127.0.0.1", outside_port)["connected"]

        print("Checking baseline connectivity before requesting firewall changes...", flush=True)
        report["before"] = {"host_ipv4": probe(gateway, servers[0].server_port),
                            "host_ipv6": probe(host6, servers[1].server_port, "6"),
                            "internet_ipv4": check_internet(probe), "localhost": localhost(),
                            "unpublished_port": host_http(guest4, 8081)}
        require_baseline(report["before"])
        print("Baseline passed: host IPv4, host IPv6, IPv4 Internet/DNS, and localhost forwarding.", flush=True)
        if options.baseline_only:
            report["success"] = True
            report["firewall_requested"] = False
            return
        firewall_requested = True
        privileged_command = ["/usr/bin/sudo", "/usr/bin/python3", str(ROOT / "tests/network_isolation.py"), "--apply", str(directory)]
        if sys.stdin.isatty():
            print("macOS requires administrator authentication for the temporary PF rules. Enter your password in this terminal.", flush=True)
            firewall_process = subprocess.Popen(privileged_command)
        else:
            print("Run this in a separate Mac terminal (outside the Linux box):", flush=True)
            print(shlex.join(privileged_command), flush=True)
            print("The password stays in that terminal. This test waits up to 5 minutes for authentication.", flush=True)
        deadline = time.monotonic() + 300
        while not (directory / "active").exists():
            if firewall_process is not None and firewall_process.poll() is not None:
                raise RuntimeError("Administrator authentication or the PF stage failed. No isolation claim is made.")
            if (directory / "firewall-error").exists():
                raise RuntimeError((directory / "firewall-error").read_text())
            if time.monotonic() >= deadline:
                raise TimeoutError("Administrator authentication was not completed. No isolation claim is made.")
            time.sleep(0.5)
        report["with_rules"] = {"host_ipv4": probe(gateway, servers[0].server_port),
                                 "host_ipv6": probe(host6, servers[1].server_port, "6"),
                                 "internet_ipv4": check_internet(probe), "localhost": localhost(),
                                 "unpublished_port": host_http(guest4, 8081)}
        current = report["with_rules"]
        report["checks"] = {"host_ipv4_blocked": not current["host_ipv4"]["connected"],
                            "host_ipv6_blocked": not current["host_ipv6"]["connected"],
                            "internet_and_dns_work": current["internet_ipv4"]["connected"],
                            "selected_localhost_port_works": current["localhost"],
                            "unpublished_port_blocked": not current["unpublished_port"]["connected"]}
        if not all(report["checks"].values()):
            raise RuntimeError("A required isolation check failed.")
        print("With rules: Mac IPv4/IPv6 blocked; IPv4 Internet and localhost forwarding work.", flush=True)
        (directory / "done").touch()
        deadline = time.monotonic() + 20
        while not (directory / "cleaned").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Firewall cleanup has not completed; check the administrator terminal.")
            time.sleep(0.25)
        report["firewall"] = json.loads((directory / "firewall-result.json").read_text())
        report["after_removal"] = {"host_ipv4": probe(gateway, servers[0].server_port),
                                   "host_ipv6": probe(host6, servers[1].server_port, "6"),
                                   "internet_ipv4": check_internet(probe), "localhost": localhost(),
                                   "unpublished_port": host_http(guest4, 8081)}
        report["checks"]["main_rules_unchanged"] = report["firewall"]["main_rules_unchanged"]
        report["checks"]["main_nat_unchanged"] = report["firewall"]["main_nat_unchanged"]
        report["checks"]["test_rules_removed"] = not report["firewall"]["remaining_rules"].strip()
        report["checks"]["pf_enable_reference_released"] = not report["firewall"]["cleanup_errors"]
        report["checks"]["host_access_restored_after_cleanup"] = all(report["after_removal"][key]["connected"] for key in ["host_ipv4", "host_ipv6"])
        report["success"] = all(report["checks"].values())
        print(json.dumps(report, indent=2), flush=True)
    except BaseException as error:
        report["success"] = False
        report["error"] = str(error)
        raise
    finally:
        if firewall_requested:
            (directory / "done").touch(exist_ok=True)
        for server in servers:
            server.shutdown()
            server.server_close()
        with (directory / "control.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if box_created:
                container("stop", name, check=False)
                report["test_container_cleaned"] = container("delete", name, check=False).returncode == 0
            if network_created:
                report["test_network_cleaned"] = container("network", "delete", network, check=False).returncode == 0
        if firewall_process is not None:
            firewall_process.wait(timeout=10)
        destination = options.report
        destination.write_text(json.dumps(report, indent=2) + "\n")
        print("Results: " + str(destination), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, TimeoutError, ValueError, OSError) as error:
        print("Network test: " + str(error), file=sys.stderr)
        sys.exit(1)
