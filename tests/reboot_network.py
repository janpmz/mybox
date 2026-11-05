"""Check real connections from a protected box after Mac startup.

A separate, temporary container confirms that each destination accepts connections.
The test never disables, reinstalls, or directly refreshes the firewall.
"""
import ipaddress
import json
import os
import re
import socket
import socketserver
import threading
import uuid

from interface_changes import run, wait_for
from network_isolation import PROBE, check_internet

REQUIRED_CHECKS = (
    "destinations_reachable_before", "mac_address_blocked", "mac_gateway_blocked",
    "mac_ipv6_blocked", "lan_endpoint_blocked", "internet_and_dns_work", "destinations_reachable_after",
    "control_container_deleted", "control_network_deleted",
)

# Only a completed connection timeout counts as blocked. A failed exec, refused
# connection, invalid address, or missing route must not produce a passing result.
TCP_PROBE = r'''use strict;
use warnings;
use Socket qw(AF_INET AF_INET6 SOCK_STREAM IPPROTO_TCP inet_pton pack_sockaddr_in pack_sockaddr_in6);
my ($host, $port, $family, $timeout) = @ARGV;
my $domain = $family eq '6' ? AF_INET6 : AF_INET;
my $address = inet_pton($domain, $host) or die "invalid IP address\n";
my $scope = 0;
if ($family eq '6' && $host =~ /^fe[89ab]/i) {
    open my $index, '<', '/sys/class/net/eth0/ifindex' or die "interface: $!\n";
    $scope = <$index>;
    chomp $scope;
    die "invalid interface index\n" unless $scope =~ /^\d+$/;
}
my $destination = $family eq '6' ? pack_sockaddr_in6($port, $address, $scope) : pack_sockaddr_in($port, $address);
socket(my $socket, $domain, SOCK_STREAM, IPPROTO_TCP) or die "socket: $!\n";
eval {
    local $SIG{ALRM} = sub { die "MYBOX_TIMEOUT\n" };
    alarm $timeout;
    connect($socket, $destination) or die "connect: $!\n";
    alarm 0;
};
my $error = $@;
alarm 0;
close $socket;
if ($error eq "MYBOX_TIMEOUT\n") { print "MYBOX_TCP timeout\n"; exit 10; }
die $error if $error;
print "MYBOX_TCP connected\n";
'''


def probe(identifier, host, port, family="4"):
    result = run("container", "exec", identifier, "/usr/bin/perl", "-e", TCP_PROBE,
                 host, str(port), family, "3", check=False, timeout=15)
    outcome = {(0, "MYBOX_TCP connected"): "connected",
               (10, "MYBOX_TCP timeout"): "timeout"}.get((result.returncode, result.stdout.strip()), "error")
    return {"host": host, "port": port, "family": family, "outcome": outcome,
            "exit_code": result.returncode, "output": result.stdout, "error": result.stderr}


def lan_configuration(target=None):
    """Select the router, or one explicit endpoint on a directly attached IPv4 LAN."""
    port = None
    if target:
        try:
            address, port = target.split(":")
            address = str(ipaddress.IPv4Address(address))
            port = int(port)
            if not 1 <= port <= 65535:
                raise ValueError()
        except ValueError:
            raise RuntimeError("Use MYBOX_TEST_LAN_TARGET=IPv4:port for one reachable local device.") from None
    route = run("/sbin/route", "-n", "get", address if target else "default").stdout
    interface_match = re.search(r"^\s*interface: ([a-z][a-z0-9]+)\s*$", route, re.MULTILINE)
    gateway_match = re.search(r"^\s*gateway: (\d+\.\d+\.\d+\.\d+)\s*$", route, re.MULTILINE)
    if not interface_match or (not target and not gateway_match):
        raise RuntimeError("Cannot select a local IPv4 device. Set MYBOX_TEST_LAN_TARGET=IPv4:port and run the test again.")
    interface = interface_match.group(1)
    if not target:
        address = gateway_match.group(1)
    device = ipaddress.IPv4Address(address)
    configuration = run("/sbin/ifconfig", interface).stdout
    addresses = re.findall(r"\binet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-f]+|[0-9.]+)", configuration)
    for host, mask in addresses:
        mask = str(ipaddress.IPv4Address(int(mask, 16))) if mask.startswith("0x") else mask
        network = ipaddress.IPv4Network(f"{host}/{mask}", strict=False)
        if device in network and device not in (network.network_address, network.broadcast_address, ipaddress.IPv4Address(host)):
            if interface.startswith(("lo", "utun", "vmenet")) or device.is_multicast or device.is_loopback:
                break
            return {"interface": interface, "mac_address": host, "subnet": str(network),
                    "address": str(device), "ports": [port] if port else [443, 80, 53]}
    raise RuntimeError("The target must be a separate device on a directly attached IPv4 LAN.")


def select_endpoint(identifier, configuration, attempts):
    for port in configuration["ports"]:
        result = probe(identifier, configuration["address"], port)
        attempts.append(result)
        if result["outcome"] == "connected":
            return configuration["address"], port
    raise RuntimeError("No tested LAN port accepted a connection. Set MYBOX_TEST_LAN_TARGET=IPv4:port for a reachable local device.")


def host_ipv6(gateway):
    sections = re.split(r"(?=^[^\s:]+: flags=)", run("/sbin/ifconfig").stdout, flags=re.MULTILINE)
    for section in sections:
        if re.search(r"\binet " + re.escape(gateway) + r"\s", section):
            address = re.search(r"\binet6 (fe80:[0-9a-f:]+)%[^\s]+ prefixlen", section)
            if address:
                return address.group(1)
    raise RuntimeError("Cannot find the Mac's IPv6 link-local address on the box bridge.")


def ipv6_ready(identifier):
    addresses = run("container", "exec", identifier, "/bin/cat", "/proc/net/if_inet6").stdout
    for row in addresses.splitlines():
        fields = row.split()
        if len(fields) == 6 and fields[5] == "eth0" and fields[0].startswith("fe80"):
            # Linux must finish duplicate-address detection before it can connect.
            return not (int(fields[4], 16) & (0x40 | 0x08))
    return False


def isolation_checks(before, protected, after, internet):
    targets = ("mac_address", "mac_gateway", "mac_ipv6", "lan_endpoint")
    return {
        "destinations_reachable_before": all(before.get(key, {}).get("outcome") == "connected" for key in targets),
        "mac_address_blocked": protected.get("mac_address", {}).get("outcome") == "timeout",
        "mac_gateway_blocked": protected.get("mac_gateway", {}).get("outcome") == "timeout",
        "mac_ipv6_blocked": protected.get("mac_ipv6", {}).get("outcome") == "timeout",
        "lan_endpoint_blocked": protected.get("lan_endpoint", {}).get("outcome") == "timeout",
        "internet_and_dns_work": internet.get("connected") is True,
        "destinations_reachable_after": all(after.get(key, {}).get("outcome") == "connected" for key in targets),
    }


def passed(report):
    return report.get("success") is True and all(report.get("checks", {}).get(key) is True for key in REQUIRED_CHECKS)


class Listener(socketserver.BaseRequestHandler):
    def handle(self):
        # A TCP handshake is sufficient. Read no data and execute no requests.
        pass


class IPv6Listener(socketserver.ThreadingTCPServer):
    address_family = socket.AF_INET6
    daemon_threads = True


def verify(identifier, report):
    suffix = uuid.uuid4().hex[:12]
    control = "reboot-control-" + suffix
    network = "reboot-control-net-" + suffix
    report.update(success=False, checks={}, cleanup={}, box_id=identifier, control_id=control,
                  control_network=network,
                  scope="TCP to Mac IPv4/IPv6 addresses and one live IPv4 LAN endpoint. IPv4 DNS and HTTP to the Internet.",
                  physical_lan_ipv6_tested=False)
    checks = report["checks"]
    servers = []
    container_attempted = network_attempted = False
    try:
        configuration = lan_configuration(os.environ.get("MYBOX_TEST_LAN_TARGET"))
        report["lan"] = configuration
        guest = json.loads(run("container", "inspect", identifier).stdout)[0]["status"]["networks"][0]
        server = socketserver.ThreadingTCPServer(("0.0.0.0", 0), Listener)
        servers.append(server)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        listener_port = server.server_address[1]

        network_attempted = True
        # Use a separate IPv6 prefix so the control network cannot overlap the box.
        control_subnet6 = f"fd{suffix[:2]}:{suffix[2:6]}:{suffix[6:10]}:1::/64"
        run("container", "network", "create", "--subnet-v6", control_subnet6, network)
        container_attempted = True
        run("container", "create", "--name", control, "--network", network, "--read-only",
            "--cpus", "1", "--memory", "256m", "--entrypoint", "/bin/sleep", "debian:trixie-slim", "infinity")
        run("container", "start", control)
        control_guest = json.loads(run("container", "inspect", control).stdout)[0]["status"]["networks"][0]
        for item in (identifier, control):
            wait_for(lambda: ipv6_ready(item), description="IPv6 address readiness in " + item)
        host6 = host_ipv6(guest["ipv4Gateway"])
        control_host6 = host_ipv6(control_guest["ipv4Gateway"])
        server6 = IPv6Listener(("::", 0), Listener)
        servers.append(server6)
        threading.Thread(target=server6.serve_forever, daemon=True).start()
        report["lan_selection_attempts"] = []
        lan = select_endpoint(control, configuration, report["lan_selection_attempts"])
        report["lan_endpoint"] = {"address": lan[0], "port": lan[1]}
        destinations = {"mac_address": (configuration["mac_address"], listener_port),
                        "mac_gateway": (guest["ipv4Gateway"], listener_port), "lan_endpoint": lan,
                        "mac_ipv6": (host6, server6.server_address[1], "6")}
        control_destinations = dict(destinations, mac_gateway=(control_guest["ipv4Gateway"], listener_port),
                                    mac_ipv6=(control_host6, server6.server_address[1], "6"))
        # Each container uses its own host gateway. Both addresses reach this same listener.
        print(f"Checking blocked access to the Mac and LAN device {lan[0]}:{lan[1]}.", flush=True)
        before = report["control_before"] = {key: probe(control, *target) for key, target in control_destinations.items()}
        if not all(value["outcome"] == "connected" for value in before.values()):
            raise RuntimeError("The temporary test container could not reach every destination. Blocking is not proven.")
        protected = report["protected_box"] = {key: probe(identifier, *target) for key, target in destinations.items()}

        def internet_probe(host, port, phase_timeout=12):
            result = run("container", "exec", identifier, "/usr/bin/perl", "-e", PROBE,
                         host, str(port), "4", "1", str(phase_timeout), check=False, timeout=phase_timeout * 3 + 10)
            return {"connected": result.returncode == 0, "output": result.stdout, "error": result.stderr}

        internet = report["internet"] = check_internet(internet_probe)
        after = report["control_after"] = {key: probe(control, *target) for key, target in control_destinations.items()}
        checks.update(isolation_checks(before, protected, after, internet))
        if not all(checks.values()):
            raise RuntimeError("Connection checks failed: " + ", ".join(key for key, value in checks.items() if not value))
    except BaseException as error:
        report["error"] = repr(error)
        raise
    finally:
        # Remove only the unique resources created by this test. Record each failure.
        actions = []
        if container_attempted:
            actions.extend([("control_container_stopped", ("container", "stop", control)),
                            ("control_container_deleted", ("container", "delete", control))])
        if network_attempted:
            actions.append(("control_network_deleted", ("container", "network", "delete", network)))
        for key, args in actions:
            try:
                result = run(*args, check=False)
                report["cleanup"][key] = result.returncode == 0
                if result.returncode:
                    report.setdefault("cleanup_errors", {})[key] = result.stderr
            except Exception as error:
                report["cleanup"][key] = False
                report.setdefault("cleanup_errors", {})[key] = repr(error)
        for server in servers:
            server.shutdown()
            server.server_close()
        for key in ("control_container_deleted", "control_network_deleted"):
            checks[key] = report["cleanup"].get(key, False)
    if not all(checks.get(key) is True for key in REQUIRED_CHECKS):
        report["error"] = "The test did not remove all temporary resources."
        raise RuntimeError(report["error"])
    report["success"] = True
