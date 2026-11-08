# MyBox

Project plan from the conversation dated 3 November 2026.
This document records the design, prototype implementation, and technical checks.

## Program names and separation

The normal command is now `mybox`. The dedicated firewall program is
`mybox-firewall-manager`. They are separate executables with separate command
handlers. The firewall manager contains no box creation, shell, import, or export
commands. The programs share only common support code and network-policy data.

`make build` produces `.build/mybox`, `.build/mybox-firewall-manager`, and `.build/mybox-port-forwarder`.
The development launcher and PATH link are named `mybox`.
`mybox setup-network` installs the dedicated manager under
`/Library/PrivilegedHelperTools/mybox-firewall-manager`. It updates the existing
Mac startup job and removes the old combined executable after successful setup.
Saved firewall policy and box data remain in place. Replacing the protected
installed executable requires administrator authentication.

All three programs compile. The live-port work adds command, firewall, and TCP relay tests.
The protected startup sequence also uses the automatic refresh implementation described below.
The user completed `mybox setup-network` on 4 November 2026.
The installed firewall manager reports version 0.1.0.
The older dated sections retain the previous `box` command name as history.

## Live port changes: 4 November 2026

`mybox ports add <name> --inside-port N --outside-port N` adds a TCP mapping to an existing box.
`mybox ports remove <name> --outside-port N` removes a MyBox mapping.
`mybox ports list <name>` reports the mapping owner and its active or saved state.
Changes to a running box do not restart its VM or Linux processes.
Changes to a stopped box remain saved until its next start.

New boxes use the same MyBox helper for mappings supplied during creation.
The CLI no longer passes these mappings to Apple's `--publish` option.
Original Apple mappings remain in the policy and listing. Their removal is unsupported.
The CLI rejects removal of an Apple mapping without changing the box.

The Swift helper runs on the Mac as the normal user, with one process per mapped box.
Network.framework listeners bind only to `127.0.0.1` and forward to the inspected box IPv4 address.
Each listener permits at most 128 concurrent connections. Each direction waits for its send callback before reading another 64 KiB.
The helper preserves buffered data when a connection state change arrives before its final receive callback.
It supports TCP half-close and retains existing connections while new listeners bind.

Settings and helper status live in `~/.config/mybox/ports/<container-id>/` with private directory permissions.
The creation timestamp prevents settings from applying to a different box that reuses a name.
A process lock prevents duplicate helpers. Atomic files and a recovery journal cover interrupted CLI changes.
The helper checks the current creation time, boot time, state, network, and IP every second.
Inspection has a three-second timeout. A failed check closes forwarding.
MyBox closes the helper before stopping the box and restores it through all normal startup paths.
After a helper failure, `mybox start <name>` restores forwarding without restarting a running box.

Firewall protocol 3 adds `update-ports`. It permits only a port-list change to an existing active network policy.
Adding a port preserves connection states. Removing the last mapping for an inside port clears Mac-to-box states for that box.
macOS `pfctl` provides source/destination state removal without a port selector.
Other incoming clients may need to reconnect. The box's outgoing connections remain in place.
Pending state cleanup is persistent, so the watcher retries it after a failure.
Run `mybox setup-network` once to install the updated manager.

`make ports-test` checks live add/remove, HTTP access, loopback binding, conflicts, connection preservation, helper recovery, and restart persistence.
It creates a disposable box and installs no Linux packages.
It records results in `test-results/ports-latest.json` and checks existing boxes and the main PF/NAT rules.
All 82 automated tests passed, including the real TCP relay tests on Mac loopback.
The complete run took 95.707 seconds.
The test now labels the expected conflict and saves each completed check immediately.
Cleanup errors no longer hide the original failure or prevent the final report.
Three additional test-runner checks passed.

After the firewall protocol 3 update, all 10 live checks passed on 4 November 2026.
Live add/remove, occupied-port recovery, helper recovery, and restoration after a box restart passed.
Adding a mapping and rejecting an occupied port preserved an existing TCP connection.
Removing one of two mappings for the same inside port preserved the remaining connection.
The document-player box retained its original start time. The main PF and NAT rules remained unchanged.
Cleanup removed the disposable box, its network, and its port settings.
See [the accepted live report](test-results/2026-11-04-live-ports-passed.json).

## Box copies: 4 November 2026

`mybox copy <source-name> <new-name>` copies the complete Linux filesystem through
Apple `container commit`. The source keeps its running or stopped state.
The new box remains stopped and retains the source's CPU and memory limits.
It receives a separate network and hostname. It does not inherit port mappings.
Normal `start` and `enter` commands still require the trusted helper and firewall checks.

Copies include installed tools, home folders, project files, symlinks, and saved credentials.
They do not include running processes or unsaved memory.
The local snapshot image remains the copy's base. Apple creates its writable disk on first start.
The trusted firewall helper continues to use the original Debian image.
Validation rejects existing destination names and unsupported source configurations.
A failed copy attempts to remove its unused image and newly created network.

Seven copy checks passed. All 101 tests passed across the initial run and targeted reruns.
The initial sandbox blocked eight terminal and local-socket tests. Those eight passed outside the sandbox.
The requested live copy from `document-player-box` to `photo-box` succeeded.
The source retained its original start time, and the copy remained stopped.
The comparison covered 20,013 filesystem entries. All contents and permissions matched except two active Codex log files.
This check inspected the snapshot image. It did not start the copied box.
See [the copy report](test-results/2026-11-04-copy-photo-box.json).

## Automatic loopback forwarding: 4 November 2026

MyBox mappings now support IPv4 TCP servers on `127.0.0.1`, `0.0.0.0`, and the box's network address.
The Mac listener remains restricted to `127.0.0.1`.
The helper first attempts a direct connection to the box's network address.
A connection error or a 300 ms connection timeout starts the loopback path.
MyBox checks the box's creation time, start time, network, and IP again before that connection.
It selects the path before sending request bytes and never retries after connecting to a server.

The loopback path runs a Perl relay through `container exec --interactive` without a terminal.
It uses the existing Debian tools and opens no extra guest listener.
Each connection gets its own relay. Framed binary input and output preserve TCP half-close in both directions.
Actual stdin EOF cancels the guest relay, including after a TCP half-close.
Nonblocking Mac pipes and bounded buffers keep a slow client from blocking other mappings or identity checks.
Closing a mapping closes its connections and cancels its relays.

Forwarding status now includes protocol version 2. CLI operations that restore forwarding replace older helpers automatically.
The CLI temporarily removes the forwarding target and waits for the old helper to release its lock.
It then restores the target and starts the new helper. It does not signal a PID from an old status file.
This update closes existing forwarded connections once. It preserves Linux processes and saved port mappings.
The privileged firewall manager does not need an update for this feature.

Live tests use temporary servers in an existing running box and a temporary Mac helper.
They change no saved mapping, firewall rule, or application configuration.
HTTP, WebSockets, large binary transfers, and half-close passed with both server bindings.
The existing photo app still used a direct network connection.
All 119 regression tests passed. All 13 live checks passed, including guest relay cancellation after helper shutdown.
See [the live report](test-results/2026-11-04-loopback-passed.json).

The photo app was then switched back to `127.0.0.1:8080` and retained that setting in its development command.
The mapped page, Vite client, and app script returned HTTP 200 through `http://localhost:8767/`.
A browser reload displayed the photo interface. Direct access to the box's network address on port 8080 failed, confirming use of the loopback path.
The box retained its original start time. See [the photo app check](test-results/2026-11-04-photo-loopback.json).

## Current implementation: trusted helper and PF

The user approved implementing the trusted-helper startup sequence on 4 November
2026. The CLI now creates a separate network for each new box. A fresh helper
creates its bridge before the development VM starts. MyBox applies and checks
interface-based PF rules, starts the development VM, then removes the helper.
The helper uses the existing Debian image, one CPU, and 256 MiB of memory.

`create`, `start`, `enter`, `import`, and `export` use the same protected startup
path. A failed PF check prevents guest startup. MyBox also checks the policy
before operating on an already running box. Incoming rules match the bridge
and any source address. This implementation does not require guest capability
drops or an IPv6 kernel argument. The PF policy blocks IPv6 and permits IPv4
Internet traffic, including the explicitly configured public DNS servers.

`mybox setup-network` installs a root-owned firewall helper, saved policy state,
and a Mac startup job. It uses a dedicated PF anchor and its own enable reference.
It preserves the main PF/NAT rules and refuses unsupported main rules.
Every start still checks protection. After a box stops, MyBox removes its active
rules so a reused bridge number does not affect an unrelated network.
The saved policy and automatic startup configuration remain installed.
This is development setup, not release packaging.

Implementation files: [Sources/Network.swift](Sources/Network.swift),
[Sources/Firewall.swift](Sources/Firewall.swift),
[Sources/FirewallManagerMain.swift](Sources/FirewallManagerMain.swift), and [Sources/Box.swift](Sources/Box.swift).
The launcher rebuilds after changes to any Swift source. Test substitutions are
compiled only into `.build/mybox-test`; the production executable has none.

Validation so far:

- Swift compilation, PF syntax checks, and all 29 offline tests pass. The terminal tests require access to their private test terminals and passed outside the tool sandbox.
- Tests check ordering, failed apply/verify operations, helper cleanup, and
  refusal to execute guest commands when protection is missing.
- The live baseline passed with the same listeners, first workload, spoofed
  source probe, hostname, Internet/DNS, and port checks used by the protected test.
- The baseline also proved that the fresh read-only helper starts with 256 MiB.
  All baseline fixtures were removed. The existing demo remained running.
- [Baseline results](test-results/2026-11-04-protected-baseline.json) record these checks.
- The user ran the complete PF test successfully on 4 November 2026.
  The persistent firewall helper and Mac startup job are installed.
- The first workload could not connect to the Mac on initial startup or restart.
  The baseline connected to the same listener. Raw sockets remained available,
  but packets with the tested false source address did not reach the Mac.
- Internet/DNS, selected inside ports, hostname access, and localhost forwarding
  worked. The unpublished port was blocked while its server was confirmed running.
- Connections between the two test boxes were blocked in both directions.
  The tested connection to a Mac IPv6 listener was also blocked.
- The helper was removed after each start. Both test boxes and networks were
  removed after the test. The main firewall and NAT rules were preserved, and
  the original running demo remained running.
- The startup restoration command passed without rebooting the Mac.
  Rule counters were collected after that command reloaded the MyBox rules,
  so those counters do not cover every earlier connection check.
- [Protected startup results](test-results/2026-11-04-protected-pf.json) preserve the evidence.
- All 14 CLI integration checks passed with the installed firewall setup.
  These cover file/folder transfers, persistence, automatic and explicit starts,
  hostname updates, localhost HTTP, and existing transfer restrictions.
  The integration box and network were removed. This run did not open a browser.
  [CLI results](test-results/2026-11-04-cli-protected.json).
- Later Mac reboot checks include a live router connection test, described below.

Existing interfaces use dynamic PF address expressions, including dormant interfaces.
The watcher update below refreshes policies automatically when host interfaces change.
The earlier acceptance tests do not establish protection during arbitrary Mac network changes.

Older boxes on Apple's shared network retain their data and configuration.
MyBox can list and stop them, but refuses to start or enter them without a
dedicated network. Automatic migration is not implemented.
The old acceptance box `mybox-cli-check-b1e503f2` was deleted at the user's request
on 4 November 2026. `mybox list` then showed no boxes.

### Import behavior update: 4 November 2026

The user requested removal of all import filters based on file names.
Import now includes `.env` files, credentials, key files, `.git`, and dependency
folders. This also applies when the user selects one of these paths directly.
Import still skips symlinks and special files. Neither import nor export replaces
an existing destination. The earlier transfer results below record the old behavior.

### Automatic network refresh: 4 November 2026

The user approved automatic detection and refresh, with an acknowledged detection
interval. Normal interface changes must not require a box restart or refusal of
the next command. This implementation keeps the existing PF networking design.

- The installed launchd job runs `mybox-firewall-manager watch` with `KeepAlive`.
- SystemConfiguration notifications and routing-socket events request a refresh.
  A five-second periodic check covers missed notifications and retries failures.
- Events use a bounded 50 ms coalescing interval. This is not a bound on total
  detection or recovery time. Scheduling, lock contention, and PF commands add delay.
- The manager compares interface names, IPv4 addresses, netmasks, and live box bridges.
  Helper membership changes alone do not interrupt connections.
- PF replaces only the MyBox anchor. The manager never clears its rules before replacement.
- State cleanup targets newly blocked destinations on each box's bridge, including
  connections with spoofed sources. It avoids clearing unrelated host connections.
- `verify` also refreshes changed policies automatically. Actual refresh failures
  still prevent commands from claiming that protection succeeded.
- On refresh failure, the manager attempts to block both directions of box networking.
  It keeps the VM running and retries. Recovery clears old states before allowing traffic.
  If PF itself cannot apply the block, the manager logs a critical failure.
- `/var/db/mybox/watcher.json` records refresh status and timing. The manager's
  `status` command displays it with administrator access.
- Protocol version 2 requires installation of the updated manager. Existing saved
  policies retain storage schema version 1 and require no data migration.

All 39 offline tests passed. The two private-terminal tests passed outside the
sandbox. New tests cover interface addition/removal, address/netmask changes,
connection cleanup, failure blocking, recovery, and refresh without a CLI operation.
No Node.js or dependencies were installed.

The user installed the watcher through `make interface-test` on 4 November 2026.
The installed executable matches the local build, and launchd reports the watcher running.
The first live run passed automatic refresh, old-connection cleanup, new host-connection
blocking, continued box operation, published-port access, and Internet/DNS access.
The observed time from watcher resume to completed refresh was 0.235 seconds.
The test deliberately paused the watcher first, so this is not a general latency bound.

The run then timed out in the test's interface-removal assertion. That assertion
searched raw JSON for a name, so `bridge1` also matched `bridge100`. The corrected
assertion compares exact interface names and requires a healthy snapshot. The test
also isolates child stdin and restores blocking terminal output to prevent a
secondary `BlockingIOError` from hiding the original result. Four regression tests pass.
The box, its network, and the temporary interface were removed. The initial report
is preserved in `test-results/2026-11-04-interface-changes-initial.json`.
The corrected live test passed all 12 checks on 4 November 2026.
Automatic refresh closed the existing connection and blocked new host connections.
The box kept the same boot ID. Commands, published ports, and Internet/DNS continued
to work. Interface removal triggered another successful refresh. The main PF and
NAT rules were preserved, and all four cleanup checks passed.

The observed resume-to-refresh time was 0.2327 seconds. Interface removal completed
its refresh in 0.2255 seconds. These measurements describe this run and do not set
a maximum delay. The successful report is preserved in
[the live acceptance results](test-results/2026-11-04-interface-changes-passed.json).
Later Mac reboot checks include a live router connection test. Those results follow below.
This implementation does not claim continuous isolation during the detection interval.

### Mac reboot checks: 4 November 2026

`tests/reboot.py prepare` records the Mac boot-session UUID, boot time, and hashes
of the installed firewall manager and launchd configuration. `verify` requires
a different boot session. It reads launchd, fresh watcher status, saved policy
boot state, the current snapshot, PF enable status, and MyBox's PF enable reference.
These checks precede all box operations and do not install, restore, or refresh PF.

After automatic recovery passes, the test starts Apple's container service only
if needed and runs the existing CLI integration checks. It records the results
in `test-results/reboot-latest.json` and `test-results/reboot-cli-latest.json`.
The actual Mac restart passed every automatic firewall recovery check before any
box operation. All 14 CLI checks then passed, and the temporary box was deleted.
The complete test failed only on its main PF and NAT rule comparisons.
The original results remain in
[the first reboot report](test-results/2026-11-04-reboot-initial.json) and
[the CLI report](test-results/2026-11-04-reboot-cli-initial.json).

The original comparison included Apple container service startup. That service was
stopped after the Mac restart, so the test started it before the CLI checks.
The report did not retain the rule text, so it cannot identify the exact changes
or establish which program caused them.

The corrected test records main rules before service startup, before box operations,
and after box operations. It reports service-start differences separately. Main
rules must remain exactly equal during the box operations. The report retains
rule text and differences even if the CLI test fails or exceeds its time limit.
Eight offline checks pass. They cover startup evidence, separate service-start
changes, strict rule comparisons, and failure reports.

The corrected live test passed all 15 checks and all 14 CLI checks on 4 November 2026.
Main PF and NAT rules remained identical during box operations. The temporary box
was deleted. The saved details from before the restart remain unchanged.
The successful results remain in
[the corrected reboot report](test-results/2026-11-04-reboot-passed.json) and
[the corrected CLI report](test-results/2026-11-04-reboot-cli-passed.json).

The rerun used the same Mac boot session. Apple's container service was already
running, so this run did not repeat service startup or identify the original rule
changes. The first run established automatic firewall recovery before any box operation.

This test covers a fresh box after reboot. It does not measure early-boot packet
timing or check existing-box storage across a Mac restart.

### Added connections to the reboot test: 4 November 2026

The previous successful reboot reports did not test blocked connections. The reboot
test now requires connection checks from the fresh box before its file and command checks.
It checks a temporary Mac listener through the Mac's IPv4 LAN address, the box's
IPv4 gateway, and the Mac's IPv6 address on the box bridge. Those connections must
time out. A connection to a live IPv4 LAN endpoint must also time out. DNS lookup
and an HTTP response from the Internet must succeed.

A temporary container on a separate network must reach the destinations before
and after the blocked attempts. Both containers use their respective Mac gateways
to reach the same listener. The test does not disable protection or reinstall the
firewall. It contacts only the router's TCP ports 443, 80, and 53 until one connects,
or one local endpoint supplied through `MYBOX_TEST_LAN_TARGET=IPv4:port`.
An unavailable destination fails the test. Failed commands, missing probes, refused
connections, and route errors cannot count as successful blocking.

The report includes targets, individual outcomes, errors, Internet attempts, and
cleanup results. It requires deletion of the temporary container and network.
This checks one physical IPv4 LAN endpoint, not every LAN device or protocol.
It also does not test a physical LAN device through IPv6.

All 18 reboot-related offline checks pass. A live check also confirmed that the
test rejects an unprotected container. That container reached the Mac through
IPv4 and IPv6, reached the router at `192.168.178.1:443`, and retained Internet access.
Both temporary containers and networks were deleted. Results appear in
[the probe check](test-results/reboot-network-unprotected-probe.json).
The test waits for Linux to finish IPv6 address checks before sending probes.
The temporary control network uses a separate IPv6 prefix.

The full protected run passed on 4 November 2026: all 16 reboot checks, all 15 CLI
checks, and all 9 connection and cleanup checks. The fresh box could not connect
to the Mac's IPv4 LAN address, its IPv4 gateway, or the Mac's IPv6 link-local address.
It also could not connect to the router at `192.168.178.1:443`. Each attempt timed out.
The separate test container reached the destinations before and after those attempts.
The protected box resolved `example.com` and received `HTTP/1.1 200 OK` on its first attempt.
Main PF and NAT rules remained unchanged. Both test containers and their networks were deleted.

This run used the existing post-reboot session with Apple's container service already running.
It proves the tested connection behavior after reboot. It does not measure packets
during early Mac startup or cover every local device and protocol. Results remain in
[the reboot connection report](test-results/2026-11-04-reboot-network-passed.json) and
[the corresponding CLI report](test-results/2026-11-04-reboot-network-cli-passed.json).

### Authentication failure and fix

The first protected test stopped during administrator authentication. It timed
out after 120 seconds, before installing the helper or startup job. Its baseline
fixtures were removed, and the existing demo remained running.

A separate test reproduced the cause: Foundation `Process` placed `sudo` in a
process group outside the terminal's foreground job. The test redirected stdin,
so the password process did not receive the expected terminal ownership.
The visible prompt did not establish a working hidden-input session.

Administrator commands now use `posix_spawn` without changing the process group.
MyBox also refuses authentication from a background terminal job. `sudo` alone
reads the terminal and controls password echo. The installation step no longer
has the test's 120-second command timeout.

Two regression tests use dummy text in private test terminals. They check
foreground ownership, hidden input, and echo restoration after success or failure.
A separate check used the real macOS `sudo` prompt and cancelled with Ctrl+C.
It entered no password, confirmed echo was disabled at the prompt, and confirmed
that echo was restored afterward. No helper was installed and no PF rules changed.
[Terminal check results](test-results/2026-11-04-auth-terminal.json).

### PF compatibility correction

The next run authenticated successfully. Setup then rejected normal Apple PF
entries because the check expected only one filter-anchor line. The actual Mac
rules include scrub anchors and a later Internet Sharing anchor.
The corrected check accepts this layout and still rejects earlier bypass rules.
A focused check passed against the exact output supplied by the user.
The production executable was rebuilt; the full suite was not repeated for this change.

The failed run copied the helper and startup plist but did not change PF rules.
Setup now checks compatibility before installing those files. The later successful
run replaced the installed helper. Both the live PF test and CLI integration
test passed. Expected test failures now report a
short error instead of a Python traceback or an empty duplicate sudo error.

The dated sections below preserve earlier results and decisions. Statements that
automatic isolation is not implemented describe those earlier stages.

## Goal

Create a small macOS tool for isolated development with untrusted npm and Python packages.
Keep projects inside boxes so the user cannot accidentally execute their dependencies from an ordinary macOS project directory.
Keep the normal macOS terminal, browser, and editor experience.

Build for personal use first, while keeping eventual public distribution as a
requirement. End users must be able to install MyBox and run `mybox` without this
source checkout, a Swift compiler, or a development-time launcher.

## Technical direction

- Use Apple's `container` CLI as the backend, with Containerization and Virtualization.framework underneath.
- Give each project a lightweight Linux VM with private storage. Keep project files in `/workspace` on the container's default storage for the initial CLI as well as the tests. Do not add a separate project disk.
- Keep one Linux VM per project during normal use. Use a temporary trusted helper to create the bridge before startup. The implemented sequence stops the helper after the development box starts. The complete PF startup test passed on initial startup and restart.
- Use a stable hostname as the connection destination, including for future SSH access. A permanent numeric IP is no longer required. Prefer Apple's built-in DNS and keep address allocation in the unmodified backend. New boxes register `<name>.box.test` as a persistent network alias; it follows the current guest address. Host DNS setup and firewall enforcement are separate concerns.
- Start with Debian Slim for Node.js and Python compatibility.
- The first Swift CLI now wraps Apple's CLI. Use `mybox` from any Mac terminal after the one-time PATH setup in [README.md](README.md). The local `./mybox` launcher also works from this folder.
- Build with the installed Swift compiler, Foundation, and Darwin. Use no third-party Swift packages.
- Use existing local tools. Do not install Node.js or dependencies during project setup.

## Isolation requirements

- Keep source files, dependencies, home directories, caches, and temporary files inside each box.
- Do not mount host folders into boxes. Do not mount project filesystems as ordinary macOS directories.
- Keep host files, credentials, Keychain access, and SSH agents unavailable to project processes by default.
- Move files only through explicit import and export operations controlled from macOS. Use `container cp` without shared folders or automatic synchronization.
- Check file-transfer behavior, including symlinks. Copy the selected files and folders without exclusions based on their names.
- Avoid `container machine` defaults that the discussion identified as sharing the host home directory and SSH agent.

Proposed network policy:

| Connection | Default |
| --- | --- |
| Box to Internet | Allow outbound public IPv4 and IPv6 traffic from any application, without a proxy or a port allowlist |
| Box to macOS services | Block |
| Box to local network | Block |
| Box to other boxes | Block |
| macOS to box | Allow only explicitly selected inside ports; published outside ports bind only to localhost |

Apple's default networking does not establish this policy automatically.
The prototype must check enforcement, including access to host services.
Internet access lets malicious code transmit data and credentials available inside its box.

### Bridge experiment on 5 November 2026

On macOS 26.7.2, an endpoint in the fork's vmnet plugin created a bridge before a disposable Debian VM booted. The VM used the same bridge after the bridge client disconnected. The bridge disappeared after the VM stopped.

Live testing stopped after the Mac restarted unexpectedly. Local crash reports record two Apple `InternetSharing` aborts at 13:22 and 13:27 CET and an iBoot panic at 13:29 CET. The panic report does not identify the cause.

After the restart, the private MyBox service switched to fork commit `44a7d72a` on branch `codex/mybox-bridge`. Its reported install root was `/Library/PrivilegedHelperTools/com.mybox.service/runtime`. All three installed executables matched the signed build, the firewall verification socket existed, and `mybox list` succeeded. The service reported zero running containers. This installation check did not boot Linux or test bridge stability, firewall enforcement at boot, or public IPv6.

### Shared fork installation on 5 November 2026

Apple’s 1.6.1 package and the separate MyBox runtime directory were removed. The replacement package, `io.github.janpmz.my-container-box`, installs the release build of fork commit `36245146` once under `/usr/local`. The signed-in account and `_mybox` each reported server version `1.6.1-mybox.1` from that directory. The signed-in account retained five stopped containers. The private account had no boxes, owned its mode-0700 home directory, and answered `mybox list`. The installer verified both account services. A subsequent [live guest test](test-results/shared-fork-live-latest.json) booted Linux 6.18.35 through MyBox. Public IPv4 TCP, UDP DNS and an HTTP request by hostname worked. The guest timed out when connecting to the Mac’s LAN and bridge IPv4 addresses, the Mac’s link-local IPv6 address and the router’s IPv4 port 443. Mac control connections reached each listener before and after the guest probe.

Public IPv6 TCP and UDP failed inside the box even though both worked from the Mac. At the time of the route check Linux had no usable IPv6 default route on its network interface. The MyBox firewall in that run blocked all IPv6 on the box bridge. This run tested an already booted guest; it did not capture packets during boot or test a separate IPv6 LAN device. The test removed its box and dedicated network.

### Public IPv6 in the shared fork

On 5 November, the installed release `1.6.1-mybox.4` and MyBox firewall version 5 passed [25 live checks](test-results/shared-ipv6-firewall-latest.json). Linux accepted vmnet’s router advertisement and learned the bridge’s link-local IPv6 gateway. TCP connections to port 443 and DNS queries over UDP received replies from public endpoints on both IPv4 and IPv6.

The first program inside the disposable box sent identifiable IPv4 and IPv6 packets toward the physical router. A capture on the Mac’s physical interface saw the control packets before and after boot and neither guest marker. Connections to the tested Mac, router, and peer-box listeners timed out from the box.

The test added two routes covering public IPv6 to a temporary VPN tunnel. Only connections explicitly bound to that tunnel used those routes. Public TCP and UDP worked from the running box over the Mac’s ordinary connection before, during, and after the test tunnel was active. The Mac also reached a TCP listener behind the tunnel, while the firewall counted three dropped packets from the box to that listener. Testing internet traffic through an external VPN provider requires a separate connection. Both boxes, their networks, and the temporary tunnel were removed.

### Network-change test plan

Apply the destination policy before the box can send its first packet. Block connections to the Mac, other boxes, and local networks reachable through physical or VPN interfaces. This includes local devices with globally routable addresses. Allow the IPv6 control traffic needed to reach the Internet.

Use a disposable box on a network with working public IPv6 to check the fork:

1. Force IPv6 for a TCP connection and a UDP request to public test endpoints. Require a reply to each over IPv6 so that IPv4 fallback cannot hide a failure.
2. Probe IPv6 listeners on the Mac, a physical LAN device, a VPN destination, and another box. Include link-local, unique-local, and globally addressed local targets. Confirm that a control client can reach each listener before and after the blocked probe. Record any missing target as untested.
3. Repeat the probes at box startup and after Wi-Fi and VPN changes. Check that an existing connection to a destination stops passing traffic if that destination becomes local.

The [earlier reboot test](test-results/2026-11-04-reboot-network-passed.json) used the IPv4-only firewall. The live test of the new firewall covered a physical router and a temporary VPN route. Switching Wi-Fi during an established IPv6 connection and reaching a server behind a real VPN remain untested.

## MVP

Start with a CLI that manages one box per project.

| Command | Purpose |
| --- | --- |
| `mybox create <name> [--inside-port N --outside-port N]` | Create and start a box with private persistent storage |
| `mybox enter <name> [-- <command> ...]` | Enter its Linux shell or run a command |
| `mybox import <name> <host-path>` | Recursively copy a file or whole folder into `/workspace` |
| `mybox export <name> <box-path> <new-host-path>` | Recursively copy a file or whole folder to a new macOS path |
| `mybox start <name>` | Start an existing box |
| `mybox stop <name>` | Stop a box without losing its project files |
| `mybox ports add <name> --inside-port N --outside-port N` | Add a saved mapping without restarting the box |
| `mybox ports remove <name> --outside-port N` | Remove a MyBox mapping without restarting the box |
| `mybox ports list <name>` | List mappings, owners, and status |
| `mybox list` | List boxes and their state |

Run package installation, development commands, and project tools inside the box.
The CLI supports explicit localhost TCP port forwarding during creation and through the `ports` commands.
`--inside-port` names the server port inside Linux; `--outside-port` names the
localhost port on the Mac, which always binds to `127.0.0.1`. Specify both flags
for each mapping. Repeated flags are paired by their occurrence order.
After creation, the CLI prints the inside and outside ports, with the outside
port labelled `localhost only`. It does not print a browser URL or a network
status message.

## Development order

1. Check the available tools and the backend's macOS and hardware requirements.
2. Prove one persistent box using Apple's CLI before writing the Swift wrapper.
3. Check import, shell access, project execution, export, and persistence after a restart.
4. Check filesystem isolation, network restrictions, and localhost forwarding.
5. Implement the small Swift CLI around the workflow from the prototype.

## Prototype status

Tests on 3 and 4 November 2026 used `container` 1.6.1 on macOS 26.7.2, ARM64.

- The storage test box, `mybox-one-disk-test`, uses `debian:trixie-slim` with the default storage and no extra project disk.
- Linux commands, direct import, and direct export passed.
- Imported files and files created inside Linux survived a stop and start.
- `container cp` works directly with `/workspace` in this setup. File transfers do not require a temporary directory inside the box.
- The storage test box remains stopped.
- The storage test box has no host folder mounts, forwarded SSH agent, or published ports.

### Firewall test results

The box used `192.168.64.11` during the test.
The Mac ran a test web server at `192.168.178.54:52725`.

I tested this temporary PF rule:

```pf
block drop in quick inet from 192.168.64.11 to 192.168.178.54 label "mybox_test_mac"
```

| Test stage | Box to Mac test server | Box to Internet (`example.com`) |
| --- | --- | --- |
| Before adding the rule | Connected | HTTP 200 |
| With the rule active | Connection timed out after 5 seconds | HTTP 200 |
| After removing the rule | Connected | HTTP 200 |

PF recorded five blocked packets.
The Mac could still read the test server's response while the rule blocked the box.

I removed the temporary rule and stopped the test server and box.
The main firewall rules remained unchanged.
I did not install a helper program.

This test covered one IPv4 address on the Mac.
[Raw test results](test-results/2026-11-03-pf.json) preserve the measurements and cleanup checks.

### Proposed rule for the home network

The following rule remains untested:

```pf
block drop in quick inet from 192.168.64.11 to 192.168.178.0/24 label "mybox_home"
```

`192.168.178.0/24` covers addresses from `192.168.178.0` through `192.168.178.255`.
This rule would block connections from the box to the Mac and other devices with addresses in that range.

The box's address may change after a restart. Use its current address when applying the rule.
This rule covers IPv4 only. The home network also uses IPv6, which requires corresponding rules and tests.

The user chose to skip the home-network test for now.
Other local devices, IPv6, and the Mac's other addresses remain untested.

### Localhost test: 4 November 2026

The test box `mybox-localhost-test` runs a small web server using tools already present in the Debian image.
No packages were installed.

The test used this port mapping:

```text
Mac: 127.0.0.1:8765 -> box: 8080
```

- A request from the Mac to `http://localhost:8765/` returned HTTP 200 and the expected test page.
- The page also displayed correctly in the Codex in-app browser.
- The box uses its default storage, with no host folder mounts or SSH-agent forwarding.
- The published port binds to `127.0.0.1` on the Mac.
- I left the test box running so the user can view the page.

Stop the test box with:

```bash
container stop mybox-localhost-test
```

[Test results](test-results/2026-11-04-localhost.json) and [browser screenshot](test-results/2026-11-04-localhost.jpg).

### Swift CLI: 4 November 2026

The first working CLI is implemented in [Sources/Box.swift](Sources/Box.swift).
The executable launcher is `./box`; a symlink at `/opt/homebrew/bin/box` makes
`box` available from any Mac terminal folder. The launcher resolves symlinks
and rebuilds after source changes. `make build` compiles `.build/box` using only
the installed Swift toolchain. No Node.js or dependencies were installed.

- Commands: `create`, `enter`, `import`, `export`, `list`, `start`, and `stop`.
- Boxes keep project files in `/workspace` on their default container storage.
- Files and whole folders can be imported and exported recursively, including ordinary hidden files, nested folders, spaces in names, and binary files.
- Import skips common secret files, dependency folders, symlinks, and special files. Export is restricted to `/workspace` and refuses symlinks and special files. Neither direction intentionally replaces or merges an existing destination.
- Boxes are marked with `com.mybox.managed=true`; the CLI leaves earlier manually created tests alone.
- Initial defaults: 2 CPUs and 2 GiB, configurable with `--cpus` and `--memory`. These have not been benchmarked. The acceptance test used 512 MiB.
- The CLI creates no host folder mounts, separate project disks, SSH-agent forwarding, or published host sockets. Published TCP ports bind to localhost only.
- `enter`, `import`, and `export` start a stopped box if needed. Interactive sessions replace the wrapper process so terminal input, signals, and exit codes pass through.

Validation completed:

| Check | Result |
| --- | --- |
| Swift compilation and ten offline CLI tests, including inside/outside port validation | Passed |
| Recursive file/folder transfer, exclusion rules, binary/hidden files, and paths containing spaces/quotes | Passed |
| Refusing existing destinations and symlink exports | Passed |
| Running commands in `/workspace`, piped input, and propagating exit code 37 | Passed |
| Stop/start persistence and automatic start during export | Passed |
| Published localhost HTTP before and after restart | Passed |
| Interactive Bash, terminal detection, Ctrl+C, and a clean exit | Passed |
| Demo page displayed in the Codex in-app browser | Passed |

The acceptance box `mybox-cli-check-b1e503f2` ran its demo server at
`http://localhost:50076/` (Mac `127.0.0.1:50076` to Linux port `8080`).
The box was deleted at the user's request on 4 November 2026.

[Acceptance results](test-results/2026-11-04-cli.json) record the checks.
Run `make test` for offline checks or `make integration` for the real workflow.
The latter creates and removes only its own new throwaway box.

Automatic Mac/LAN/other-box blocking is not implemented. This version is a
workflow prototype; the next implementation milestone is enforcing the network
policy before using the boxes with untrusted packages. File transfer during
concurrent hostile modification has not been audited.

### Network isolation work in progress: 4 November 2026

Packaging remains deferred. The current work is a reversible end-to-end network
policy experiment in [tests/network_isolation.py](tests/network_isolation.py),
run with `make network-test`. The normal CLI's networking has not been changed
or declared isolated.

- Apple's `--internal` network option is host-only and would remove the Internet
  access the project needs. A dedicated NAT network plus host-side filtering is
  being evaluated instead.
- Apple's backend creates the host bridge/VM interface only when a box starts,
  not when its network or stopped container is created. A production design must
  establish protection before untrusted workloads run and retain it over restarts.
- The experiment scopes PF rules to its dedicated bridge and VM interface, so
  matching does not rely only on a guest-supplied source IP address. It blocks
  connections to dynamic host addresses, directly connected networks, private
  and special IPv4 destinations, and all IPv6 traffic. IPv4 Internet access uses
  explicitly configured public DNS for this test.
- The selected localhost TCP port gets stateful host-to-box access. Other new
  host-to-box connections are blocked on the dedicated test network.
- The test starts only trusted fixture code before filtering. It therefore does
  not yet establish a safe startup sequence for existing untrusted boxes.
- macOS PF requires administrator authentication. A separate, temporary root
  stage applies one test anchor, holds its own PF enable reference, and removes
  both after the checks. It neither reloads `/etc/pf.conf` nor installs a helper.

The administrator-authenticated test passed on 4 November 2026. The preserved
[PF acceptance results](test-results/2026-11-04-network-pf.json) record:

| Connection | Before rules | With rules | After removal |
| --- | --- | --- | --- |
| Box to Mac IPv4 test service | Connected | Timed out | Connected |
| Box to Mac IPv6 test service | Connected | Timed out | Connected |
| Box to IPv4 Internet and DNS | HTTP 200 | HTTP 200 | HTTP 200 |
| Published localhost port | Worked | Worked | Worked |

PF counted four blocked IPv4 packets and four blocked IPv6 packets on the test
bridge. Test rules and the test's own PF enable reference were removed; the main
filter/NAT rules were unchanged. Both the throwaway box and network were removed.
This earlier experiment did not probe physical LAN devices. Later reboot checks tested a live router connection.

The successful run exercised the private-IPv4 and IPv6 blocking rules. Its
host-address, selected-port, and unpublished-port rules recorded no matches, so
the run does not establish their effectiveness. The revised test version uses
explicit dynamic host-interface addresses instead of `(self)`, filters incoming
traffic on the parent bridge rather than potentially reusable child interface
names, and adds a direct unpublished-port check. Those revised rules subsequently
passed with PF applied; see the follow-up results below.

At this stage startup protection was unresolved. The interface policy needs a host bridge name,
but Apple's CLI creates the interface while starting the VM. Applying filtering
after startup permits a gap in which guest code could make connections. The
test used only trusted fixtures during that gap. On restart, previously modified
guest system files cannot be assumed trusted. At that stage, the user rejected a
keeper VM. The later helper proposal below reopens this approach.
A source-range experiment with backend-enforced capability
restrictions and IPv6 disabled at boot passed the pre-start sequence, as recorded
below. Automatic CLI integration is still unfinished.

Fixed-address alternative: the user proposed a fixed IP or hostname. A dedicated
fixed subnet can identify the box's address range before its host interface
exists, so the interface-based timing limitation does not rule out pre-start PF
filtering. A throwaway network reserved `192.168.250.0/24` and
`fd34:5b90:9c61:1::/64`; both ranges were available from `container network
inspect` before any VM started, and no host interface appeared. The network was
removed afterward. [Reservation results](test-results/2026-11-04-fixed-subnet.json).

Apple's installed CLI exposes fixed network subnets, but does not advertise a
direct per-container `--ip` option. A dedicated subnet would let PF cover the
entire range without requiring one exact guest IP. A fixed hostname is a DNS
convenience and does not authenticate packet sources. Before relying on
source-range rules, verify that guest traffic cannot evade them by using an
address outside its assigned subnet. The later source-spoofing probe below
demonstrated that the default guest can do this. A subsequent pre-start PF test
passed with capability restrictions and IPv6 disabled at boot; those restrictions
are a required part of that candidate, not optional hardening.

The earlier exact-IP investigation is superseded by the stable-hostname direction.
Do not pursue a custom allocator, backend fork, `/etc/bootptab`, or a `/30`
pool-sizing patch for hostname support. Historical results remain in
[test-results/2026-11-04-tiny-subnet.json](test-results/2026-11-04-tiny-subnet.json):
`/30` failed to allocate and `/29` worked. A fixed subnet may still be useful for
pre-start filtering, but is not an identity guarantee. Apple's reserved vmnet
backend disables DHCP and assigns addresses itself, so DHCP reservations do not
apply to this backend.

Offline tests also cover refusing physical/loopback interfaces and invalid
inputs, scoped rule cleanup, releasing the test's own PF reference even after
an error, and refusing stale test requests. The ordinary CLI still does not
enable network isolation.

A subsequent user-run baseline timed out only on the external Internet probe;
Mac IPv4/IPv6 and localhost passed, and no PF changes were requested. The probe
previously shared a four-second budget across DNS, TCP, and HTTP. It now records
the failing phase, uses separate time limits, and retries the Internet check
once. Every attempt is retained; a persistent failure still prevents PF changes.
`--baseline-only` permits diagnosis without administrator authentication. A
fresh baseline reproduction passed before the probe change. The improved probe
then passed a complete baseline-only run, with no PF changes and confirmed
cleanup, recorded separately in `test-results/network-baseline-diagnostic.json`.
All 18 offline tests pass, including transient retry and persistent-failure checks.

## Stable hostnames and firewall follow-up: 4 November 2026

Apple `container` client and service are both 1.6.1. The running demo was left
untouched. No global service restart, backend modification, dependency install,
shared folder, SSH forwarding, or packaging work was performed.

### DNS implementation and observed behavior

New `box create <name>` calls attach to `default,alias=<name>.box.test`. The
container ID remains `mybox-<name>`. This is a fully qualified **exact alias**,
so it does not need `[dns] domain` in `~/.config/container/config.toml` or a
container-service restart. It also works on custom networks, as tested directly.
The CLI's existing inside/outside port output is unchanged, and it prints no
browser URL. New names must end with a letter or digit to form a valid hostname;
existing boxes can still be managed using their original names.

macOS applications require one administrator setup command:

```sh
sudo /usr/local/bin/container system dns create box.test
```

This writes `/etc/resolver/containerization.box.test`, pointing `box.test` at
Apple's loopback DNS server on port 2053, and signals mDNSResponder. It does not
restart the container service. Do not use `--localhost`: that selects a different
DNS handler and does not resolve network aliases. The old `dns default set`
command is not available in 1.6.1. The config.toml default-domain method remains
valid for automatically qualifying container IDs, but is unnecessary here.

The user installed the resolver on 4 November 2026. macOS `getaddrinfo` then
resolved the custom-network alias to the current IPv4 and IPv6 addresses on
first start and both restarts, including actual IP changes. The in-app browser
displayed the hostname test page on inside port 8080; the same fixture also
responded through its published localhost port. Both tests cleaned up their
fixtures. [System DNS results](test-results/2026-11-04-dns-system.json) and
[browser results](test-results/2026-11-04-dns-browser.json).
The host-side resolver requires authentication even though registering aliases
on new containers does not. Existing boxes do not gain aliases retroactively;
Apple stores aliases at creation and does not expose an alias-update command.
Keep existing project data and boxes intact.

[Apple's versioned DNS architecture](https://github.com/apple/container/blob/1.6.1/docs/architecture/dns.md)
and [networking guide](https://github.com/apple/container/blob/1.6.1/docs/networking.md)
describe these mechanisms. `--dns-domain` configures guest DNS search behavior;
it does not change the registered hostname.

The disposable [DNS test](tests/dns.py) passed on a custom network, using direct
queries to `127.0.0.1:2053`:

- Both A and AAAA answers matched the inspected guest addresses.
- The IPv4 address changed from `192.168.65.2` to `.4`, then `.5`; the exact same
  friendly alias followed both restarts.
- A stopped box returned no records, but its alias remained reserved: a second
  container's attempt to claim it was rejected.
- The alias persisted in container configuration, and all test resources were
  removed. The temporary second VM was only an allocation-change test fixture,
  not a keeper VM or part of the proposed box architecture.

[Raw DNS results](test-results/2026-11-04-dns-direct.json). Run `make dns-test`
for direct-server checks, or `python3 tests/dns.py --system` after resolver setup
for macOS `getaddrinfo` checks. Apple documents a five-second DNS TTL; applications
can cache old addresses briefly. Mac/backend restart persistence is supported by
the stored configuration design but has not been tested by restarting this Mac
or its global container service.

All 18 offline tests and the full CLI integration workflow passed after the
alias change. The integration run verified aliases on the default network before
and after restart, file/folder imports and exports, persistence, and unchanged
localhost forwarding. It removed its throwaway box.
[CLI regression results](test-results/2026-11-04-cli-dns.json).

### How names interact with ports

`myproject.box.test` resolves to the **guest IP**, not `127.0.0.1`. With inside
port 8080 and outside port 8766, direct hostname access addresses port 8080;
`127.0.0.1:8766` still uses the localhost mapping. DNS does not translate ports or
restrict access. The intended firewall policy permits direct host access only
to selected inside ports, rejects unpublished ports, and retains localhost-only
outside bindings. Until those rules are enforced, a guest service on an
unpublished port is still reachable from the host by IP or hostname. Do not
interpret DNS support or port publishing alone as network isolation.

### Source spoofing and startup protection

A new [controlled source-address probe](tests/source_spoof.py) sent marked UDP
packets only to a temporary listener on the Mac's test-network gateway. With
the backend's default capabilities, the listener received a packet claiming
source `198.18.0.2`, outside the assigned `192.168.65.0/24`. Therefore vmnet does
**not** enforce the assumed IPv4 source-subnet boundary on this path. Installing
only source-subnet PF rules before startup would be bypassable by ordinary guest
root using raw sockets. No physical LAN devices were probed.

Repeating with `--cap-drop NET_RAW --cap-drop NET_ADMIN` rejected raw-socket
creation with `Operation not permitted`, including the same-subnet control.
[Probe results](test-results/source-spoof-latest.json). This is evidence for
capability restriction, not proof against every spoofing technique or a guest
kernel compromise. These capability restrictions are not yet enabled by MyBox.

A separate throwaway guest started successfully with
`--kernel-arg ipv6.disable=1` and those capability drops. `/proc/cmdline` contained
the setting and `/proc/sys/net/ipv6` was absent. Its capability bounding set also
excluded NET_RAW and NET_ADMIN. [Boot probe](test-results/ipv6-boot-latest.json).
This offered a small candidate for an IPv4-only policy established before guest
startup. The subsequent end-to-end test below verified its startup sequence,
DNS/AAAA behavior, and IPv4 connectivity.

The first revised PF attempt expired without authentication. A subsequent
user-authenticated run **passed**. Host IPv4/IPv6 access and the direct unpublished
port were blocked; IPv4 Internet/DNS and selected localhost forwarding worked.
Unlike the earlier experiment, counters now show actual selected-port traffic
(11 packets), unpublished-port blocks (4), host-address blocks (4), and IPv6
blocks (7). The main PF/NAT rules were unchanged, the test anchor and its enable
reference were removed, both fixtures were deleted, and access returned after
cleanup. [Preserved revised PF results](test-results/2026-11-04-network-revised-pf.json).
This remains an after-start, interface-scoped test; it does not prove startup
protection or physical LAN-device isolation.

The pre-start experiment is [tests/prestart_network.py](tests/prestart_network.py).
It reserves a dedicated subnet, tests a trusted baseline, removes that baseline
guest, creates a new stopped guest, verifies the bridge is absent, installs
source-range rules, then starts the new guest. The initial workload itself
attempts a host connection and records its capability bounding set and failed
raw/packet/IPv6 socket attempts. It also tests unprivileged `IP_FREEBIND` against
its own host UDP listener. The experiment repeats after stop/start and checks
direct hostname access to the selected port, rejection of an unpublished port,
localhost forwarding, and Internet/DNS. It stops guests before removing PF.

Its no-PF baseline passed: raw and packet sockets were denied, IPv6 sockets were
unavailable, and an IP_FREEBIND-bound out-of-subnet UDP socket could not transmit
(`Network is unreachable`). Both selected and unpublished servers were reachable,
and Internet, macOS DNS, and localhost forwarding worked.
[Pre-start fixture baseline](test-results/2026-11-04-prestart-baseline.json).
Apple still returns an AAAA record for the allocated address even when guest IPv6
is disabled; the tested HTTP client successfully falls back to IPv4. The rule
syntax and all 19 offline tests pass.

The user-authenticated **pre-start PF test passed** on 4 November 2026.
[Preserved results](test-results/2026-11-04-prestart-pf.json) establish:

- The dedicated guest bridge did not exist when rules were prepared, and PF
  was active before the new VM's first start.
- The first workload recorded `host_connected=0` on both initial start and
  restart. The no-PF baseline recorded `host_connected=1` against the same host
  listener. Different kernel boot IDs verify fresh first-workload observations.
- The raw/packet/IPv6 socket restrictions and failed IP_FREEBIND transmission
  were observed in the first workload on both boots.
- Host access remained blocked after startup, while Internet/DNS, the direct
  selected hostname port, and localhost forwarding worked. A separately verified
  running server on unpublished port 8081 was inaccessible from the host.
- The IPv4 address changed from `.3` to `.4` on restart and the alias followed it.
  The same pre-installed source-range policy covered both addresses without an
  interface name or a permanent numeric guest IP.
- Guests were stopped before cleanup. The test anchor and enable reference
  were removed; main PF/NAT rules were unchanged and all fixtures were deleted.

This establishes a working startup sequence for the tested restricted guest
configuration. It is not host-side source authentication: the policy depends
on the guest kernel enforcing the capability drops and IPv6 boot setting.
It does not prove isolation from a compromised guest kernel, physical LAN
devices, or every spoofing mechanism. Other-box and host-network-change tests
remain outstanding.

Reliable automatic isolation is still **not implemented**. Do not enable it
in the CLI by copying the temporary test's timed cleanup mechanism. Capability/IPv6 restrictions
must be applied by the backend before any workload, never by a script in mutable
`/workspace`. Integration still needs consistent
protection for create/start and automatic starts, persistent rule ownership,
other-box checks, and handling of network changes. Physical LAN-device testing
remains deliberately deferred.

Agreed persistent-policy direction: keep MyBox's PF rules active independently
of whether a box is running. Restore the saved policy and acquire MyBox's PF
enable reference automatically at Mac startup. The user explicitly agreed that
every MyBox startup path must verify protection before starting the guest and
refuse to start if protection is absent. This verification is required even with
automatic boot restoration. These lifecycle guarantees are not implemented yet.

Preferred filtering boundary: interface-based PF rules matching incoming traffic
from any source address on the box's actual bridge. This avoids using a guest's
claimed source address as its identity. The user prefers this approach; keep the
subnet/capability experiment as an evaluated alternative, not a requirement.
The existing interface-scoped PF test passed. Before adopting it automatically,
establish reliable interface selection and protection before the guest's first
traffic. Do not assume every box uses `bridge100`; one verified test used
`bridge102`. Merely discovering the bridge and adding rules after startup does
not close the initial protection gap.

### Temporary helper proposal and bridge test

The user proposed starting a trusted helper on the box's network before starting
the development box. The helper creates the bridge. MyBox could then identify
that bridge, load its PF policy, check protection, and start the development box.
The policy must match incoming traffic from any source address on that bridge.

A lifecycle test on 4 November 2026 used two disposable sleeping containers and
the existing Debian image. It changed no PF rules and installed no dependencies.
[Helper bridge results](test-results/2026-11-04-helper-bridge.json) record:

- Network creation alone exposed no bridge with the reserved gateway address.
- Starting the helper exposed `bridge101` with one VM interface.
- Starting the development fixture added a second VM interface to that bridge.
- Stopping the helper preserved the bridge and the development fixture's interface.
- After both fixtures stopped, no bridge had the test gateway address.
- Restarting the helper and development fixture repeated the shared-bridge behavior.
- Cleanup removed both fixtures and their network. The existing demo remained running.

This supports a temporary helper: stop it only after the development box joins
the protected bridge. Before each later start, repeat bridge discovery and check
the PF policy. Do not treat the observed bridge number as a permanent identity.
If any protection check fails, do not start the development box.

The helper must use trusted code and must not share project storage.
The `sleep` process makes no network requests, but this does not establish zero
traffic from the guest kernel or network setup.
The combined helper-before-PF-before-workload sequence has not yet been tested
with PF active. This lifecycle test alone does not prove isolation.
Other-box checks and reliable automatic rule ownership remain necessary.

## Distribution

- A combined local test package is built by `make package` as
  `dist/MyBox-0.1.0-arm64.pkg`. It includes the desktop UI from the build's source
  snapshot, compiled CLI and helpers, and the separate fork runtime. Native
  preinstall/postinstall support checks the owner and stopped workloads and sets
  up the private services without a terminal-only setup step. It targets Apple
  silicon and macOS 26+. The package has not yet been installed for validation;
  Developer ID signing, notarization, and public release remain deferred. See
  [packaging documentation](packaging/README.md) and the generated verification report.
- The current `/opt/homebrew/bin/mybox` link is a development convenience tied to
  this checkout. It is not the release installation method, and Homebrew must
  not become a requirement just because that folder is used on this Mac.
- Distribute precompiled `mybox`, the desktop app, and the Swift service helpers. Initial builds target Apple
  silicon. Define and test the supported macOS versions explicitly before a
  public release.
- The proposed initial delivery is a Developer ID-signed, notarized `.pkg`
  installer that always installs both the CLI in `/usr/local/bin/mybox` and the desktop app in `/Applications/MyBox.app`, makes
  the `mybox` command available, and supports upgrades and documented removal.
  Install the firewall manager in `/Library/PrivilegedHelperTools/mybox-firewall-manager`.
  Choose and implement the final packaging workflow when preparing a release.
- The fork package now installs in `/Library/Application Support/MyBox/Runtime`,
  with its own package receipt, service names, personal data and configuration
  directories, registry credential identifier, DNS ports, resolver filenames,
  and PF anchor. Apple's original package remains in `/usr/local`.
  Linux kernel and image downloads retain their existing on-demand behavior.
  A new setup screen is deferred.
- `my-container` invokes the private fork executable. Direct fork commands run
  under the calling Mac account; `mybox` continues to use `_mybox` and preserves
  that account's existing disks and images. The private service disables the
  runtime's host DNS listeners, leaving the fork's ports available to the
  signed-in account. The combined release installer must include the launcher.
- `scripts/migrate-isolated-runtime.sh` backs up program files, switches the
  private service, and restores Apple's verified 1.6.1 package on development
  installations that previously used the shared fork. It refuses migration if
  MyBox boxes are running. The [coexistence test](test-results/runtime-coexistence-latest.json)
  passed with Apple's 1.6.1 and the fork's `1.6.1-mybox.5`: both services ran with
  independent temporary stores, and stopping either left the other running.
  This check did not boot Linux.
- On 8 November 2026, the development Mac was migrated to those separate
  installations. Both installed runtimes passed the coexistence check while the
  private MyBox service remained available. The [migration verification](test-results/runtime-migration-latest.json)
  confirmed that installed native helpers match the build and both existing
  boxes retain their CPU, memory, disk usage, capacity, port mappings, and stopped
  state. Program backups remain at `/private/var/db/mybox-runtime-backup.zlm5xD`.
- Starting Apple's runtime with the existing personal store later exposed two
  unused `reserved-on-demand` networks left by the old shared fork. Apple's
  helper rejects that option and repeatedly exits, leaving startup waiting.
  The personal container directory was empty. Both network definitions were
  moved to `~/Library/Application Support/MyBox/MigrationBackups/personal-networks-20261108-124658`.
  Both personal runtimes then reported running, and the private MyBox inventory
  remained unchanged. The migration script now checks for incompatible personal
  networks before replacing the old fork. Temporary empty-store tests alone do
  not establish compatibility with existing saved configuration.
- Runtime Python components have been replaced by `mybox-service`, `mybox-access`,
  and `mybox-setup`, with file transfer code compiled into the service executable.
  Installed MyBox will require neither Python nor developer tools. Python remains
  a development dependency for tests and the historical network migration utility.
- Keep installation, upgrades, and removal of the program separate from box
  storage. Program updates or ordinary uninstallation must not delete project
  containers or their files.

The current compiled binary links only to macOS system libraries. A copied
executable passed `--version` and `help` from a temporary directory with only
system folders on `PATH`. This checks basic relocation; it is not a clean-Mac
installation test. Signed packaging, notarization, upgrade/removal behavior,
and testing on another Mac remain future work.

Apple's [notarization documentation](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)
describes Developer ID signing and support for flat installer packages.

## Later work

- Consider a separate project volume when projects need to outlive replacement of their containers. A Mac interface for browsing files inside Linux is also a future feature.
- Optional communication between selected boxes through private virtual networks.
- Codex CLI inside a box.
- Investigate Codex and VS Code access through restricted SSH connections. Keep project execution inside the box.
- Consider a SwiftUI interface after the CLI workflow works.

## Limits and unresolved details

- Linux boxes cannot execute native macOS applications.
- Sparse disks should grow with use up to a configured maximum. Check capacity and space-reclamation behavior before selecting defaults.
- CPU and RAM should be configurable. Measure workload performance before selecting defaults.
- Plan without direct Apple GPU or hardware video-encoder access. Rendering performance requires measurement.
- Agent authentication needs a design that limits credential exposure to untrusted project code.
- Existing apps remain alternatives, but the discussion did not establish that any app met every isolation requirement.
