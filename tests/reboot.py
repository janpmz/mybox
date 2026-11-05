"""Save startup details, then check automatic firewall recovery after a Mac restart.

This test never installs, restores, or refreshes the firewall itself. It checks
startup evidence first, then tests real connections and the CLI workflow.
"""
import argparse
import difflib
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from interface_changes import prepare_terminal_output, run, sudo
import reboot_network

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "test-results/reboot-before.json"
REPORT = ROOT / "test-results/reboot-latest.json"
CLI_REPORT = ROOT / "test-results/reboot-cli-latest.json"
HELPER = Path("/Library/PrivilegedHelperTools/mybox-firewall-manager")
PLIST = Path("/Library/LaunchDaemons/io.github.janpmz.my-container-box.firewall.plist")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def boot_seconds(value):
    match = re.search(r"\bsec\s*=\s*(\d+)", value)
    if not match:
        raise RuntimeError("Cannot read the Mac boot time.")
    return int(match.group(1))


def current_boot():
    return {"uuid": run("/usr/sbin/sysctl", "-n", "kern.bootsessionuuid").stdout.strip(),
            "time": run("/usr/sbin/sysctl", "-n", "kern.boottime").stdout.strip()}


def require_new_boot(before, after):
    if before["uuid"] == after["uuid"]:
        raise RuntimeError("The Mac has not restarted since preparation. Restart it before running this check.")


def main_rules():
    return {"filter": sudo("/sbin/pfctl", "-sr").stdout,
            "nat": sudo("/sbin/pfctl", "-sn").stdout}


def rule_changes(before, after):
    return {kind: "".join(difflib.unified_diff(before[kind].splitlines(keepends=True),
                                            after[kind].splitlines(keepends=True),
                                            fromfile="before", tofile="after"))
            for kind in before}


def startup_checks(boot, job, status, store, snapshot, pf_info, references, now):
    pid = re.search(r"^\s*pid = (\d+)\s*$", job, re.MULTILINE)
    last_check = status.get("lastCheck", 0)
    token = store.get("token", "")
    return {
        "watcher_running_automatically": bool(re.search(r"^\s*state = running\s*$", job, re.MULTILINE))
            and pid is not None and int(pid.group(1)) == status.get("pid"),
        "watcher_report_is_current": boot_seconds(boot["time"]) <= last_check <= now + 1 and now - last_check <= 30,
        "watcher_reports_no_error": not status.get("error"),
        "saved_policy_uses_current_boot": boot_seconds(store.get("boot", "")) == boot_seconds(boot["time"]),
        "snapshot_uses_current_boot": boot_seconds(snapshot.get("boot", "")) == boot_seconds(boot["time"]),
        "networking_is_not_suspended": snapshot.get("blocked") is False,
        "pf_enabled_before_box_commands": "Status: Enabled" in pf_info,
        "mybox_pf_reference_is_active": bool(token) and re.search(r"\b" + re.escape(token) + r"\b", references) is not None,
    }


def prepare():
    boot = current_boot()
    job = run("/bin/launchctl", "print", "system/io.github.janpmz.my-container-box.firewall").stdout
    if not re.search(r"^\s*state = running\s*$", job, re.MULTILINE):
        raise RuntimeError("The firewall watcher is not running before the reboot.")
    baseline = {"boot": boot, "prepared_at": time.time(), "helper_sha256": digest(HELPER), "plist_sha256": digest(PLIST)}
    BASELINE.write_text(json.dumps(baseline, indent=2) + "\n")
    print("Startup details saved. Save your work. Then restart the Mac.", flush=True)
    print("After login, run make reboot-test from the MyBox project folder.", flush=True)


def verify():
    before = json.loads(BASELINE.read_text())
    boot = current_boot()
    # Refuse before authentication or any test operation if no reboot occurred.
    require_new_boot(before["boot"], boot)
    report = {"boot_before": before["boot"], "boot_after": boot, "checks": {"mac_reboot_occurred": True},
              "success": False, "first_workload_packet_tested": False}
    try:
        checks = report["checks"]
        checks["installed_manager_unchanged"] = before["helper_sha256"] == digest(HELPER)
        checks["startup_configuration_unchanged"] = before["plist_sha256"] == digest(PLIST)
        if not all(checks.values()):
            raise RuntimeError("The installed firewall or startup configuration changed after preparation.")
        print("Checking automatic startup before any box command. Administrator authentication may be required.", flush=True)
        # sudo reads the password from /dev/tty. No firewall mutation occurs here.
        subprocess.run(["/usr/bin/sudo", "-v"], stdin=subprocess.DEVNULL, check=True)
        deadline = time.monotonic() + 15
        while True:
            try:
                job = sudo("/bin/launchctl", "print", "system/io.github.janpmz.my-container-box.firewall").stdout
                status = json.loads(sudo("/bin/cat", "/var/db/mybox/watcher.json").stdout)
                store = json.loads(sudo("/bin/cat", "/var/db/mybox/state.json").stdout)
                snapshot = json.loads(sudo("/bin/cat", "/var/db/mybox/snapshot.json").stdout)
                pf_info = sudo("/sbin/pfctl", "-si").stdout
                references = sudo("/sbin/pfctl", "-s", "References").stdout
                observed = startup_checks(boot, job, status, store, snapshot, pf_info, references, time.time())
                checks.update(observed)
                report["watcher_before_box_commands"] = status
                if all(observed.values()):
                    break
                error = "Automatic startup checks failed: " + ", ".join(key for key, passed in observed.items() if not passed)
            except (RuntimeError, OSError, ValueError) as failure:
                error = str(failure)
            if time.monotonic() >= deadline:
                raise RuntimeError(error)
            time.sleep(.5)

        rules = report["main_rules"] = {"before_container_service": main_rules()}
        print("Automatic firewall recovery passed. Checking the container service and box workflow.", flush=True)
        service_running = run("container", "list", "--all", "--format", "json", check=False).returncode == 0
        report["container_service_needed_start"] = not service_running
        if not service_running:
            run("container", "system", "start")
        # Apple service startup and MyBox operations are separate comparison periods.
        rules["before_box_workflow"] = main_rules()
        changes = report["main_rule_changes"] = {
            "container_service": rule_changes(rules["before_container_service"], rules["before_box_workflow"])}
        CLI_REPORT.unlink(missing_ok=True)
        try:
            result = subprocess.run([sys.executable, str(ROOT / "tests/integration.py"), "--network-checks", "--report", str(CLI_REPORT)],
                                    stdin=subprocess.DEVNULL, timeout=600)
        finally:
            rules["after_box_workflow"] = main_rules()
            changes["box_workflow"] = rule_changes(rules["before_box_workflow"], rules["after_box_workflow"])
            checks["main_pf_rules_preserved"] = rules["before_box_workflow"]["filter"] == rules["after_box_workflow"]["filter"]
            checks["main_nat_rules_preserved"] = rules["before_box_workflow"]["nat"] == rules["after_box_workflow"]["nat"]
        cli = json.loads(CLI_REPORT.read_text())
        report["cli_checks"] = cli["checks"]
        report["network"] = cli.get("network", {})
        checks["network_isolation_after_reboot"] = reboot_network.passed(report["network"])
        checks["box_workflow_after_reboot"] = cli["success"] and all(cli["checks"].values())
        checks["temporary_box_deleted"] = cli["final_state"] == "deleted"
        if result.returncode:
            raise RuntimeError("The CLI or connection test failed after reboot. Check the saved network results.")
        report["success"] = all(checks.values())
        if not report["success"]:
            failed = ", ".join(key for key, passed in checks.items() if not passed)
            raise RuntimeError("Checks failed after reboot: " + failed)
    except BaseException as error:
        report["error"] = repr(error)
        raise
    finally:
        REPORT.write_text(json.dumps(report, indent=2) + "\n")
        print("Results: " + str(REPORT), flush=True)
        print(json.dumps(report, indent=2), flush=True)


def main():
    prepare_terminal_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["prepare", "verify"])
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare()
    else:
        verify()


if __name__ == "__main__":
    main()
