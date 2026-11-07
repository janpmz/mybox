# MyBox for macOS

A native SwiftUI menu bar app with a compact, dark list of boxes.
No third-party dependencies.

## Build

Requires Apple silicon, macOS 14 or newer, and Swift 6. From this directory:

```sh
make build
make run
make test
```

The app is `.build/MyBox.app`, locally signed for development. The existing
MyBox CLI and private service manage the boxes. Settings can select another CLI.

## Interface

The Boxes table displays the same fields as `mybox list`: name, state, CPU,
memory, disk use, disk capacity, and ports. Port arrows run from Mac to box.
Click a `localhost:PORT` link to open that address in your default browser.
Links are available for every saved mapping; opening one does not start a stopped
box. Each box has a dedicated
Terminal button in both the table and the menu bar panel. Double-clicking a row
also enters that box. Other actions are in the row menu and right-click menu.

Forms have a Cancel button; Settings, Port Mappings, and CLI output have a Close
button. Escape dismisses every sheet. File pickers use the standard macOS controls.

| CLI operation | App |
| --- | --- |
| `list` | Boxes table; refreshes every eight seconds |
| `create` | New Box, with CPU, memory, and optional port mappings |
| `enter` | Dedicated Terminal button in both box lists |
| `enter … -- …` | Run Command; the shell command runs inside Linux |
| `start`, `stop` | Row menu |
| `copy` | Copy, with a new box name |
| `resize` | Resize on stopped boxes; blank values remain unchanged |
| `import` | Import with the macOS file picker |
| `export` | Export a workspace path to a new Mac destination |
| `ports list/add/remove` | Port Mappings, including forwarding status and ownership |
| `help`, `--version` | App menu |
| `completion zsh` | Copy Zsh Completion in the app menu |

Service and network setup are intentionally excluded. Fixed mappings owned by
Apple are displayed but cannot be removed, matching the CLI's limitation.

Operations open Terminal for the existing CLI's approvals. Progress and command
errors appear in Terminal; the table refreshes automatically. CPU and memory
are configured limits. Disk values come directly from the CLI. Neither box state
nor a saved port mapping is presented as a live firewall health check.

The menu bar icon remains after the window closes. Its panel offers the same box
actions, New Box, and an explicit Open MyBox button for the full window. Apple's
monochrome Safari compass symbol opens each box's localhost address in the default browser; if several ports
are mapped, it offers a choice of addresses. Settings contains only the CLI location.

## Verification

`make test` checks CLI table parsing, creation and resize arguments, copy names,
port status and fixed mappings, export paths, host-shell argument quoting,
process failures, and timeouts. These tests do not change real boxes.

Read-only visual preview:

```sh
make preview
open -n .build/MyBox.app --args --demo --screenshot /tmp/mybox-boxes.png
open -n .build/MyBox.app --args --demo --screenshot /tmp/mybox-ports.png --capture-view ports
```

Capture views: `menu`, `create`, `copy`, `resize`, `ports`, `export`, `command`,
`settings`, and `compact`. Each capture process exits after writing its PNG.
Preview mode is labeled and disables operations against real boxes.

`desktop/` owns the app's sources, resources, icon generator, build, and tests.
The CLI and service remain in the parent checkout. The app reads the installed
CLI's table by header positions and rejects unrecognized formats; it does not
require a privileged service upgrade.
