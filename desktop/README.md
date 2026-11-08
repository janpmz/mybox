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
MyBox CLI and private service manage the boxes. The app finds the CLI automatically.

## Interface

The Boxes window has a native title bar, a filter toolbar, and a flat, scrollable
list with shared columns for name/state, compute, disk use/capacity, port tags,
and actions. The menu bar popover uses the same charcoal body, lighter header,
darker footer, and faint dividers. The list reads `mybox list --json`.
Port arrows run from the outside port
(Mac) to the inside port (box), matching the CLI's names.
Click a `localhost:PORT` link to open that address in your default browser.
Links are available for every saved mapping; opening one does not start a stopped
box. Each box has a dedicated
Terminal button in both the table and the menu bar panel. Double-clicking a row
also enters that box. Other actions are in the row menu and right-click menu.
Start/Stop appears at the right of each main-window row and before each popover
name. Pending requests show a spinner and prevent duplicate commands until the
Terminal command exits. Terminal still starts a stopped box when needed.

The footer totals use the CLI's exact byte values: configured RAM for running
boxes and disk usage for all boxes. Missing values produce “Unavailable”, not a
partial total. “Connected to MyBox” means the list request succeeded; it is not
an engine or firewall health check. The footer gear opens the existing app menu
(CLI Help and Quit); there is no separate Settings screen.

Forms have a Cancel button; Port Mappings and CLI Help have a Close
button. Escape dismisses every sheet. File pickers use the standard macOS controls.
Memory and disk inputs contain numbers only, with GB beside the field. New Box
accepts fractional memory values such as 0.5 GB; both decimal points and commas work.

| CLI operation | App |
| --- | --- |
| `list --json` | Boxes table; refreshes every eight seconds |
| `create` | New Box, with CPU, memory, and optional port mappings |
| `enter` | Dedicated Terminal button in both box lists |
| `enter … -- …` | Run Command; the shell command runs inside Linux |
| `start`, `stop` | Start/Stop in the main list, play/stop in the popover, or row menu |
| `delete` | Delete in the row and menu bar menus, with a native confirmation alert |
| `copy` | Copy, with a new box name |
| `resize` | Resize on stopped boxes; blank values remain unchanged |
| `import` | Import with the macOS file picker |
| `export` | Export a workspace path to a new Mac destination |
| `ports list/add/remove` | Port Mappings, including forwarding status and ownership |
| `help` | App menu, with wrapped text and hidden scrollbars |

Service and network setup are intentionally excluded. Fixed mappings owned by
Apple are displayed but cannot be removed, matching the CLI's limitation.

Operations open Terminal for the existing CLI's approvals. Progress and command
errors appear in Terminal; the table refreshes automatically. CPU and memory
are configured limits. Disk values come directly from the CLI. Neither box state
nor a saved port mapping is presented as a live firewall health check.

The menu bar icon remains after the window closes. Its panel offers the same box
actions, New Box, and an explicit Open MyBox button for the full window. The
popover grows with the box list until it approaches the available screen height.
Only then does the list scroll; the header and footer remain visible. The
external-link symbol opens each box's localhost address in the default browser; if several ports
are mapped, it offers a choice of addresses.

Delete permanently removes the box and its files, saved port mappings, and dedicated
network. Running boxes stop first. Escape cancels; Return does not trigger deletion.
The app passes `--yes` after confirmation so Terminal does not ask a second time.
After updating the CLI, run `mybox setup-service` to update the installed worker
and broker before using new commands such as Delete or JSON lists.

## Verification

`make test` checks CLI JSON parsing, exact resource totals and unknown values,
Terminal command exit reporting, creation and resize arguments, copy names,
port status and fixed mappings, export paths, host-shell argument quoting,
process failures, and timeouts. These tests do not change real boxes.

Read-only visual preview:

```sh
make preview
open -n .build/MyBox.app --args --demo --screenshot /tmp/mybox-boxes.png
open -n .build/MyBox.app --args --demo --screenshot /tmp/mybox-ports.png --capture-view ports
```

Capture views: `window` (including the native title bar), `menu`, `create`, `copy`,
`resize`, `ports`, `export`, `command`, `help`, and `compact`.
Add `--demo-scenario stopped`, `many`, `empty`, `unknown`, or `error` to inspect
edge cases. Scenarios only apply with `--demo`. Each capture process exits after writing its PNG.
Interactive preview mode is labeled and disables operations against real boxes.
Screenshot captures omit the preview labels and render at twice the view's resolution.
The `window` capture includes transparent rounded corners and a small outer margin
so it displays cleanly against both light and dark page backgrounds.

The CLI and desktop app stay in one repository, and the planned installer includes both. `desktop/` owns the
app's sources, resources, icon generator, build, and tests. `Shared/BoxList.swift`
defines their JSON contract and resource formatting. The app rejects malformed
data and unsupported schema versions. The CLI works without opening the desktop app.
