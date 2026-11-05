"""Check tiny IPv4 subnet support with throwaway networks/boxes; no PF changes."""
from datetime import date
import json
from pathlib import Path
import shutil
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
CONTAINER = shutil.which("container")


def call(*arguments):
    result = subprocess.run([CONTAINER, *arguments], text=True, capture_output=True, timeout=30)
    return {"exit_code": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}


def main():
    report = {"test_date": date.today().isoformat(), "pf_changes_requested": False, "cases": []}
    for prefix in [30, 29]:
        identifier = uuid.uuid4().hex[:8]
        network = f"mybox-tiny-{prefix}-{identifier}"
        boxes = []
        case = {"prefix": prefix, "requested_subnet": f"192.168.250.0/{prefix}", "network": network, "boxes": []}
        report["cases"].append(case)
        print(f"Checking /{prefix}...", flush=True)
        try:
            case["network_create"] = call("network", "create", "--label", "io.github.janpmz.my-container-box.network-test=true",
                                          "--subnet", case["requested_subnet"], network)
            if case["network_create"]["exit_code"]:
                print(f"/{prefix}: network creation failed: " + case["network_create"]["stderr"], flush=True)
                continue
            inspected = call("network", "inspect", network)
            if inspected["exit_code"]:
                raise RuntimeError(inspected["stderr"])
            case["network_status"] = json.loads(inspected["stdout"])[0]["status"]
            for index in [1, 2]:
                name = f"mybox-tiny-box-{identifier}-{index}"
                box = {"name": name}
                case["boxes"].append(box)
                box["create"] = call("create", "--name", name, "--network", network, "--memory", "256m", "--cpus", "1",
                                     "--label", "io.github.janpmz.my-container-box.network-test=true", "--entrypoint", "/bin/sleep", "debian:trixie-slim", "infinity")
                if box["create"]["exit_code"]:
                    break
                boxes.append(name)
                box["start"] = call("start", name)
                if box["start"]["exit_code"]:
                    print(f"/{prefix}: box {index} could not start: " + box["start"]["stderr"], flush=True)
                    break
                status = json.loads(call("inspect", name)["stdout"])[0]["status"]
                box["ipv4"] = status["networks"][0]["ipv4Address"]
                print(f"/{prefix}: box {index} started at " + box["ipv4"], flush=True)
        except Exception as error:
            case["error"] = str(error)
            print(f"/{prefix}: {error}", flush=True)
        finally:
            case["box_cleanup"] = []
            for name in reversed(boxes):
                try:
                    call("stop", name)
                    case["box_cleanup"].append({"name": name, "delete": call("delete", name)})
                except Exception as error:
                    case["box_cleanup"].append({"name": name, "error": str(error)})
            try:
                case["network_delete"] = call("network", "delete", network)
                remaining = json.loads(call("network", "list", "--format", "json")["stdout"])
                case["network_removed"] = not any(item["id"] == network for item in remaining)
            except Exception as error:
                case["cleanup_error"] = str(error)
            (ROOT / "test-results" / "tiny-subnet-latest.json").write_text(json.dumps(report, indent=2) + "\n")
    print("Results: " + str(ROOT / "test-results" / "tiny-subnet-latest.json"), flush=True)


if __name__ == "__main__":
    main()
