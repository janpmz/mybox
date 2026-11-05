// Invoked in the installing user's login session by the root-owned MyBox service.
// Its stdin and stdout are private pipes to that service.
import AppKit
import Darwin
import LocalAuthentication

let input = FileHandle.standardInput.readDataToEndOfFile()
guard getuid() != 0, geteuid() == getuid(),
      let fields = try? JSONDecoder().decode([String].self, from: input),
      fields.count == 2, fields[0].utf8.count == 64, !fields[1].isEmpty else { exit(1) }
let app = NSApplication.shared
app.setActivationPolicy(.accessory)

// A long command must remain reviewable even if the system prompt wraps or
// truncates its reason. Use a scrollable view, with Cancel as the default.
if fields[1].count > 220 {
    let alert = NSAlert()
    alert.messageText = "Approve MyBox access?"
    alert.informativeText = "Review the complete request below."
    let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 500, height: 160))
    scroll.hasVerticalScroller = true
    let text = NSTextView(frame: scroll.contentView.bounds)
    text.isEditable = false
    text.isVerticallyResizable = true
    text.autoresizingMask = [.width]
    text.string = fields[1]
    scroll.documentView = text
    alert.accessoryView = scroll
    alert.addButton(withTitle: "Cancel")
    alert.addButton(withTitle: "Authenticate")
    app.activate(ignoringOtherApps: true)
    guard alert.runModal() == .alertSecondButtonReturn else { exit(1) }
}

let context = LAContext()
context.touchIDAuthenticationAllowableReuseDuration = 0
var error: NSError?
guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &error) else { exit(1) }
let reason = fields[1].count <= 220 ? fields[1] : "Approve the MyBox request you just reviewed."
context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { approved, _ in
    if approved {
        FileHandle.standardOutput.write(Data(fields[0].utf8))
    }
    exit(approved ? 0 : 1)
}
app.run()
