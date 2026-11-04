"""Apple 1.6.1 DNS aliases on a custom network; no service restart or installs.

By default queries Apple's DNS directly. --system also requires the one-time
`sudo container system dns create box.test` setup and checks macOS resolution.
All containers and the network created here are disposable and removed.
"""
import argparse
from datetime import date
import ipaddress
import json
import socket
import time
import uuid

from network_isolation import ROOT, container, command


def lookup(name, kind="A"):
    output = command(["/usr/bin/dig", "+time=2", "+tries=1", "+short",
                      "@127.0.0.1", "-p", "2053", name, kind]).stdout
    return [str(ipaddress.ip_address(line)) for line in output.splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", action="store_true")
    parser.add_argument("--report", type=lambda p: ROOT / p,
                        default=ROOT / "test-results/dns-latest.json")
    options = parser.parse_args()
    suffix = uuid.uuid4().hex[:10]
    name = "mybox-dns-" + suffix
    alias = "dns-" + suffix + ".box.test"
    network = name + "-network"
    boxes = []
    created = False
    report = {"test_date": date.today().isoformat(), "name": name, "alias": alias,
              "network": network, "system_resolver_requested": options.system,
              "service_restarted": False, "checks": {}}
    checks = report["checks"]
    try:
        container("network", "create", "--label", "com.mybox.network-test=true", network)
        created = True

        def create(box, attachment):
            container("create", "--name", box, "--network", attachment,
                      "--label", "com.mybox.network-test=true", "--memory", "256m",
                      "--cpus", "1", "--entrypoint", "/bin/sleep", "debian:trixie-slim", "infinity")
            boxes.append(box)

        def verify(stage):
            record = json.loads(container("inspect", name).stdout)[0]
            attachment = record["status"]["networks"][0]
            v4 = attachment["ipv4Address"].split("/")[0]
            v6 = str(ipaddress.ip_address(attachment["ipv6Address"].split("/")[0]))
            observed = {"ipv4": v4, "ipv6": v6, "A": lookup(alias), "AAAA": lookup(alias, "AAAA")}
            report[stage] = observed
            checks[stage + "_A"] = observed["A"] == [v4]
            checks[stage + "_AAAA"] = observed["AAAA"] == [v6]
            checks[stage + "_alias_persisted"] = alias in record["configuration"]["networks"][0]["options"]["aliases"]
            if options.system:
                deadline = time.monotonic() + 15
                addresses = set()
                while time.monotonic() < deadline:
                    try:
                        addresses = {str(ipaddress.ip_address(item[4][0])) for item in socket.getaddrinfo(alias, 80)}
                    except socket.gaierror:
                        addresses = set()
                    if addresses == {v4, v6}:
                        break
                    time.sleep(0.5)
                observed["system_addresses"] = sorted(addresses)
                checks[stage + "_system_resolver"] = addresses == {v4, v6}
            print(stage + ": " + json.dumps(observed), flush=True)
            return v4

        create(name, network + ",alias=" + alias)
        container("start", name)
        initial = verify("first_start")
        container("stop", name)
        checks["stopped_record_removed"] = lookup(alias) == [] and lookup(alias, "AAAA") == []
        # Force allocation to move while the alias owner is stopped.
        replacement = name + "-occupant"
        create(replacement, network)
        container("start", replacement)
        duplicate = name + "-duplicate"
        rejected = container("create", "--name", duplicate, "--network", network + ",alias=" + alias,
                             "--entrypoint", "/bin/sleep", "debian:trixie-slim", "infinity", check=False)
        checks["stopped_alias_cannot_be_claimed"] = rejected.returncode != 0 and "already" in rejected.stderr.lower()
        report["duplicate_error"] = rejected.stderr.strip()
        if rejected.returncode == 0:
            boxes.append(duplicate)
        container("start", name)
        checks["ip_changed"] = verify("restart") != initial
        container("stop", name)
        container("start", name)
        verify("second_restart")
        report["success"] = all(checks.values())
        if not report["success"]:
            raise RuntimeError("DNS checks failed: " + repr([key for key, value in checks.items() if not value]))
    except BaseException as error:
        report["success"] = False
        report["error"] = str(error)
        raise
    finally:
        report["cleanup"] = {}
        for box in reversed(boxes):
            container("stop", box, check=False)
            report["cleanup"][box] = container("delete", box, check=False).returncode == 0
        if created:
            report["cleanup"][network] = container("network", "delete", network, check=False).returncode == 0
        report["success"] = report.get("success", False) and all(report["cleanup"].values())
        options.report.write_text(json.dumps(report, indent=2) + "\n")
        print("Results: " + str(options.report), flush=True)


if __name__ == "__main__":
    main()
