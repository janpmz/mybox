"""Real acceptance check using only installed tools and an existing Debian image."""
import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
import uuid

from dns import lookup
import reboot_network

ROOT = Path(__file__).resolve().parents[1]
BOX = str(ROOT / "mybox")
CONTAINER = shutil.which("container")
PAGE_MARKER = "MyBox CLI is working"

HTML = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>MyBox CLI demo</title>
<style>
body{margin:0;background:#f3f4ef;color:#173c36;font:18px/1.6 system-ui,sans-serif}
main{max-width:780px;margin:12vh auto;padding:36px}
.label{font-size:14px;letter-spacing:.14em;text-transform:uppercase;color:#42685c}
h1{font-size:clamp(40px,7vw,68px);line-height:1.08;letter-spacing:-.05em;margin:20px 0 30px}
p{max-width:590px}code{background:#e2e8df;padding:3px 8px;border-radius:6px}
.status{margin-top:40px;padding:18px 22px;border:1px solid #b7c9bb;border-radius:12px;background:#eaf0e6}
</style><main><div class="label">MyBox / Linux workspace</div>
<h1>MyBox CLI is working</h1>
<p>This page was imported as part of a whole folder, changed inside Linux,
and kept after a stop and start.</p>
<p>The server is running inside a Debian container. Your Mac browser reaches
it through an explicitly published localhost port.</p>
<div class="status">Project files: <code>/workspace/sample project</code><br>
Storage: container default · Shared Mac folders: none</div></main></html>
"""

SERVER = r'''use strict;
use warnings;
use IO::Socket::INET;
my $server = IO::Socket::INET->new(LocalAddr => '0.0.0.0', LocalPort => 8080,
    Listen => 10, ReuseAddr => 1) or die "listen: $!";
while (my $client = $server->accept()) {
    eval {
        local $SIG{ALRM} = sub { die "timeout" };
        alarm 5;
        while (my $line = <$client>) { last if $line =~ /^\r?\n$/; }
        open my $file, '<', '/workspace/sample project/index.html' or die $!;
        local $/;
        my $body = <$file>;
        print $client "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: "
            . length($body) . "\r\nConnection: close\r\n\r\n" . $body;
        alarm 0;
    };
    alarm 0;
    close $client;
}
'''


def run(command, expect=0, input=None):
    result = subprocess.run(command, input=input, capture_output=True, timeout=90)
    if result.returncode != expect:
        raise AssertionError(f"{command!r}: exit {result.returncode}, expected {expect}\n"
                             + result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace"))
    return result.stdout


def box(*arguments, **kwargs):
    return run([BOX, *arguments], **kwargs)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def start_server(name):
    box("enter", name, "--", "/bin/sh", "-c",
        "nohup /usr/bin/perl '/workspace/sample project/server.pl' >/tmp/mybox-demo-server.log 2>&1 </dev/null &")


def wait_page(port):
    url = f"http://127.0.0.1:{port}/"
    for _ in range(30):
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                body = response.read().decode()
                if response.status == 200 and PAGE_MARKER in body:
                    return
        except OSError:
            pass
        time.sleep(0.2)
    raise AssertionError("Demo server did not return the expected page.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="Keep the demo box running for browser verification")
    parser.add_argument("--network-checks", action="store_true", help="Test blocked Mac/LAN connections and working Internet access")
    parser.add_argument("--report", type=Path, default=ROOT / "test-results" / "cli-latest.json",
                        help="JSON results path (default: test-results/cli-latest.json)")
    options = parser.parse_args()
    name = "cli-check-" + uuid.uuid4().hex[:8]
    identifier = "mybox-" + name
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    report = {"test_date": date.today().isoformat(), "box_name": name, "container_id": identifier,
              "host_port": port, "container_port": 8080, "url": f"http://localhost:{port}/",
              "checks": {}, "browser_verified": False}
    checks = report["checks"]
    created = False
    success = False
    with tempfile.TemporaryDirectory(prefix="mybox-integration-") as temporary:
        temporary = Path(temporary)
        try:
            project = temporary / "sample project"
            (project / "nested" / "deeper").mkdir(parents=True)
            (project / "nested" / "deeper" / "binary.dat").write_bytes(bytes(range(256)) * 32)
            (project / "nested" / "quote ' ; $.txt").write_text("argument boundaries\n")
            (project / ".hidden").write_text("hidden files work\n")
            (project / ".env.example").write_text("EXAMPLE=value\n")
            (project / ".env").write_text("DUMMY_SECRET=not-a-real-secret\n")
            (project / "node_modules").mkdir()
            (project / "node_modules" / "included.txt").write_text("included\n")
            (project / "outside-link").symlink_to(temporary, target_is_directory=True)
            (project / "index.html").write_text(HTML.replace(PAGE_MARKER, "Before editing inside Linux"))
            (project / "server.pl").write_text(SERVER)
            box("create", name, "--inside-port", "8080", "--outside-port", str(port), "--memory", "512m")
            created = True
            info = json.loads(run([CONTAINER, "inspect", identifier]))[0]
            configuration = info["configuration"]
            assert configuration["mounts"] == []
            assert configuration["ssh"] is False
            assert configuration["publishedSockets"] == []
            assert configuration["publishedPorts"] == [{"containerPort": 8080, "count": 1,
                "hostAddress": "127.0.0.1", "hostPort": port, "proto": "tcp"}]
            checks["default_storage_no_mounts_or_agent_localhost_only"] = True
            alias = name + ".box.test"
            assert configuration["networks"][0]["options"]["aliases"] == [alias]
            assert lookup(alias) == [info["status"]["networks"][0]["ipv4Address"].split("/")[0]]
            checks["friendly_dns_alias"] = True

            if options.network_checks:
                checks["network_isolation"] = False
                report["network"] = {}
                reboot_network.verify(identifier, report["network"])
                checks["network_isolation"] = reboot_network.passed(report["network"])

            output = box("import", name, str(project)).decode()
            assert "Skipped 1 symlinks or special files." in output, output
            box("enter", name, "--", "/bin/sh", "-c",
                "test -f '/workspace/sample project/.env' && "
                "test -f '/workspace/sample project/node_modules/included.txt' && "
                "test ! -L '/workspace/sample project/outside-link'")
            checks["recursive_import_includes_all_names"] = True
            box("import", name, str(project), expect=1)
            checks["import_refuses_existing_destination"] = True
            assert box("enter", name, "--", "/bin/pwd").strip() == b"/workspace"
            box("enter", name, "--", "/bin/sh", "-c", "exit 37", expect=37)
            piped = b"stdin survives through the Swift wrapper\n"
            assert box("enter", name, "--", "/usr/bin/tee", "/workspace/piped.txt", input=piped) == piped
            checks["command_workdir_stdin_and_exit_status"] = True
            box("enter", name, "--", "/bin/sh", "-c",
                "printf '%s' \"$1\" > '/workspace/sample project/index.html'", "mybox-test", HTML)
            original = box("enter", name, "--", "/bin/cat", "/workspace/sample project/index.html")
            assert PAGE_MARKER.encode() in original
            checks["changed_inside_linux"] = True

            output_folder = temporary / "whole folder result"
            box("export", name, "/workspace/sample project", str(output_folder))
            for relative in ["nested/deeper/binary.dat", "nested/quote ' ; $.txt", ".hidden", ".env", ".env.example",
                             "node_modules/included.txt", "server.pl"]:
                assert digest(project / relative) == digest(output_folder / relative), relative
            assert (output_folder / "index.html").read_bytes() == original
            checks["recursive_export_binary_hidden_and_spaced_paths"] = True
            box("export", name, "/workspace/sample project", str(output_folder), expect=1)
            assert (output_folder / "index.html").read_bytes() == original
            checks["export_refuses_existing_destination"] = True
            one_file = temporary / "one file.txt"
            box("export", name, "piped.txt", str(one_file))
            assert one_file.read_bytes() == piped
            box("import", name, str(one_file))
            assert box("enter", name, "--", "/bin/cat", "/workspace/one file.txt") == piped
            checks["single_file_import_and_export"] = True

            box("enter", name, "--", "/bin/ln", "-s", "/etc", "/workspace/outside")
            box("export", name, "outside/passwd", str(temporary / "forbidden"), expect=1)
            box("enter", name, "--", "/bin/mkdir", "/workspace/links")
            box("enter", name, "--", "/bin/ln", "-s", "/Users", "/workspace/links/host-link")
            box("export", name, "links", str(temporary / "forbidden-folder"), expect=1)
            assert not (temporary / "forbidden").exists()
            assert not (temporary / "forbidden-folder").exists()
            checks["symlink_path_and_folder_export_rejected"] = True

            start_server(name)
            wait_page(port)
            checks["localhost_http_before_restart"] = True
            box("stop", name)
            assert "stopped" in box("list").decode()
            box("stop", name)  # Already stopped is harmless.
            auto_export = temporary / "from stopped.txt"
            box("export", name, "piped.txt", str(auto_export))
            assert auto_export.read_bytes() == piped
            checks["export_starts_stopped_box_and_persistence"] = True
            box("stop", name)
            box("start", name)
            box("start", name)  # Already running is harmless.
            assert box("enter", name, "--", "/bin/cat", "/workspace/sample project/index.html") == original
            start_server(name)
            wait_page(port)
            checks["explicit_stop_start_and_localhost_after_restart"] = True
            info = json.loads(run([CONTAINER, "inspect", identifier]))[0]
            assert lookup(alias) == [info["status"]["networks"][0]["ipv4Address"].split("/")[0]]
            checks["friendly_dns_alias_after_restart"] = True
            success = True
        finally:
            if created:
                if options.keep and success:
                    report["final_state"] = "running"
                else:
                    box("stop", name)
                    run([CONTAINER, "delete", identifier])
                    run([CONTAINER, "network", "delete", "mybox-net-" + name])
                    report["final_state"] = "deleted"
            report["success"] = success
            options.report.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
