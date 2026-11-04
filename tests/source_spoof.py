"""Send marked IPv4 UDP packets only to our own host listener.

Checks whether vmnet rejects a guest source outside its reserved subnet, and
whether dropping NET_RAW and NET_ADMIN prevents this userspace probe. No PF
changes or physical LAN probes. This does not test guest-kernel compromise.
"""
from datetime import date
import json
import socket
import uuid

from network_isolation import ROOT, container

RAW = r'''
use strict;
use warnings;
use Socket qw(AF_INET SOCK_RAW IPPROTO_RAW IPPROTO_IP IP_HDRINCL inet_aton sockaddr_in);
my ($source, $target, $port, $marker) = @ARGV;
socket(my $raw, AF_INET, SOCK_RAW, IPPROTO_RAW) or die "raw socket: $!\n";
setsockopt($raw, IPPROTO_IP, IP_HDRINCL, pack('i', 1)) or die "IP_HDRINCL: $!\n";
my $udp = pack('nnnn', 43123, $port, 8 + length($marker), 0) . $marker;
my $ip = pack('CCnnnCCna4a4', 0x45, 0, 20 + length($udp), 1234, 0, 64, 17, 0,
              inet_aton($source), inet_aton($target));
send($raw, $ip . $udp, 0, sockaddr_in($port, inet_aton($target))) or die "send: $!\n";
print "sent\n";
'''


def main():
    identifier = uuid.uuid4().hex[:10]
    network = "mybox-spoof-" + identifier
    boxes = []
    created = False
    report = {"test_date": date.today().isoformat(), "network": network,
              "scope": "Own host UDP listener only; no PF changes", "cases": []}
    try:
        container("network", "create", "--label", "com.mybox.network-test=true", network)
        created = True
        status = json.loads(container("network", "inspect", network).stdout)[0]["status"]
        report["network_status"] = status
        for hardened in [False, True]:
            name = network + ("-dropped" if hardened else "-default")
            args = ["create", "--name", name, "--network", network, "--memory", "256m", "--cpus", "1"]
            if hardened:
                args += ["--cap-drop", "NET_RAW", "--cap-drop", "NET_ADMIN"]
            container(*args, "--entrypoint", "/bin/sleep", "debian:trixie-slim", "infinity")
            boxes.append(name)
            container("start", name)
            record = json.loads(container("inspect", name).stdout)[0]
            guest = record["status"]["networks"][0]["ipv4Address"].split("/")[0]
            for source in [guest, "198.18.0.2"]:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
                    receiver.bind((status["ipv4Gateway"], 0))
                    receiver.settimeout(2)
                    marker = "mybox-source-probe-" + identifier
                    sent = container("exec", name, "/usr/bin/perl", "-e", RAW, source,
                                     status["ipv4Gateway"], str(receiver.getsockname()[1]), marker, check=False)
                    result = {"capabilities_dropped": hardened, "source": source,
                              "send_exit": sent.returncode, "send_error": sent.stderr.strip(), "received": False}
                    try:
                        data, peer = receiver.recvfrom(2048)
                        result.update(received=data == marker.encode(), peer=list(peer))
                    except socket.timeout:
                        pass
                    report["cases"].append(result)
                    print(json.dumps(result), flush=True)
            container("stop", name)
    finally:
        report["cleanup"] = {}
        for box in boxes:
            container("stop", box, check=False)
            report["cleanup"][box] = container("delete", box, check=False).returncode == 0
        if created:
            report["cleanup"][network] = container("network", "delete", network, check=False).returncode == 0
        destination = ROOT / "test-results/source-spoof-latest.json"
        destination.write_text(json.dumps(report, indent=2) + "\n")
        print("Results: " + str(destination), flush=True)


if __name__ == "__main__":
    main()
