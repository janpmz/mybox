import SwiftUI

enum AppTheme {
    static let background = Color(red: 0.135, green: 0.143, blue: 0.158)
    static let surface = Color(red: 0.165, green: 0.174, blue: 0.19)
    static let separator = Color.white.opacity(0.08)
    static let accent = Color(red: 0.50, green: 0.63, blue: 0.94)
    static let running = Color(red: 0.48, green: 0.78, blue: 0.60)
}

struct BoxState: View {
    let box: Workspace
    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(box.isRunning ? AppTheme.running : Color.secondary.opacity(0.6))
                .frame(width: 5, height: 5)
            Text(box.stateLabel).foregroundStyle(.secondary)
        }.font(.system(size: 12)).accessibilityElement(children: .combine)
    }
}

struct SheetFrame<Content: View, Actions: View>: View {
    enum Dismissal { case cancel, close }

    let title: String
    var width: CGFloat = 420
    var dismissal: Dismissal = .cancel
    @Environment(\.dismiss) private var dismiss
    @ViewBuilder let content: Content
    @ViewBuilder let actions: Actions
    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            Text(title).font(.system(size: 17, weight: .semibold))
            content
            HStack(spacing: 10) {
                Spacer()
                if dismissal == .cancel {
                    Button("Cancel", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
                } else {
                    Button("Close") { dismiss() }.keyboardShortcut(.defaultAction)
                }
                actions
            }.controlSize(.large)
        }.padding(24).frame(width: width)
            .background(AppTheme.background).tint(AppTheme.accent).preferredColorScheme(.dark)
            .onExitCommand { dismiss() }
    }
}
