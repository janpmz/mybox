# Can MyBox prevent direct access through Apple’s container runtime?

Can we let a Mac user work in a MyBox Linux environment while requiring every shell session and every box start to go through MyBox? The concern is a person or process that can run commands on the Mac under the same account as MyBox.

## Why direct access works today

MyBox is a macOS command-line tool that manages Linux boxes through Apple’s `container` CLI. Both tools run under the same Mac account. In version 1.6.1, Apple’s service checks that the caller and server have the same effective user ID. A direct request from the user’s terminal passes that check. [Apple’s architecture](https://github.com/apple/container/blob/1.6.1/docs/technical-overview.md) · [Apple’s user ID check](https://github.com/apple/container/blob/1.6.1/Sources/ContainerXPC/XPCServer.swift#L175-L193)

The password prompt in `mybox enter` comes from macOS `sudo`. It authorizes the MyBox firewall helper to check or apply packet filter (PF) rules. Linux opens the shell without a separate login.

| Box state | Through MyBox | Directly through Apple’s CLI |
| --- | --- | --- |
| Running | `mybox enter demo` checks the firewall rules before opening a shell. | `container exec -it mybox-demo /bin/bash` opens a shell without that check. Existing firewall rules still apply. |
| Stopped with `mybox stop demo` | MyBox removes the box’s active rules on stop. On the next start, it restores and verifies them before Linux starts. | `container start mybox-demo` skips that sequence. Linux can start without the MyBox rules that restrict access to the Mac and its local network. |

## A design to consider

A dedicated, non-admin Mac account could own the container service, the box disks, and their configuration. The user would request operations through a MyBox service. That service would check the caller’s permissions and require firewall verification before startup. This is a proposal. We still need to test account separation and startup after reboot.

## Questions for review

1. Is a dedicated account the simplest reliable way to enforce these checks? Does Apple provide a supported way to restrict access to individual boxes under one account?
2. What other paths need controls? For example, could a user read a stopped box’s disk, copy its files through the runtime, or change its configuration?
3. If software under the user’s login can call `mybox enter`, when should MyBox require a person to approve entry or export?
4. Can this design prevent an unprotected start after a crash or reboot, before anyone logs into the service account? How would you test that?

The proposed scope trusts the Mac administrator. An administrator can use `sudo` to become the service account. If that person is also a concern, would you move the runtime to a separately administered host?
