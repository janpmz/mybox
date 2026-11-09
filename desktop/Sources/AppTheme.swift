import SwiftUI

enum AppTheme {
    static let background = Color(red: 32 / 255, green: 33 / 255, blue: 35 / 255)
    static let headerColor = NSColor(red: 43 / 255, green: 44 / 255, blue: 47 / 255, alpha: 1)
    static let header = Color(nsColor: headerColor)
    static let surface = header
    static let footer = Color(red: 25 / 255, green: 26 / 255, blue: 28 / 255)
    static let primary = Color(red: 242 / 255, green: 242 / 255, blue: 244 / 255)
    static let secondary = Color(red: 168 / 255, green: 169 / 255, blue: 176 / 255)
    static let separator = Color.white.opacity(0.06)
    static let accent = Color(red: 137 / 255, green: 182 / 255, blue: 1)
    static let primaryButton = Color(red: 10 / 255, green: 132 / 255, blue: 1)
    static let running = Color(red: 48 / 255, green: 211 / 255, blue: 107 / 255)
    static let stopped = Color(red: 133 / 255, green: 134 / 255, blue: 141 / 255)
    static let activePort = accent.opacity(0.14)
    static let inactivePort = Color.white.opacity(0.07)
    static let contentInset: CGFloat = 24
    static let rowHeight: CGFloat = 72
    static let iconSize: CGFloat = 14
    static let rowFont = Font.system(size: 14)
    static let secondaryFont = Font.system(size: 12)
    static let footerFont = Font.system(size: 12)
}

struct Hairline: View {
    @Environment(\.displayScale) private var displayScale
    var body: some View {
        AppTheme.separator.frame(height: 1 / displayScale).accessibilityHidden(true)
    }
}

struct PrimaryActionStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .semibold))
            .foregroundStyle(.white)
            .padding(.horizontal, 12).frame(height: 32)
            .background(AppTheme.primaryButton, in: RoundedRectangle(cornerRadius: 8))
            .overlay {
                RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(Color.white.opacity(0.14), lineWidth: 1)
            }
            .contentShape(RoundedRectangle(cornerRadius: 8))
            .opacity(isEnabled ? (configuration.isPressed ? 0.8 : 1) : 0.4)
    }
}

/// Native buttons retain keyboard activation and focus; only hover/press draws a fill.
struct FlatActionStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        FlatActionBody(configuration: configuration)
    }

    private struct FlatActionBody: View {
        let configuration: ButtonStyle.Configuration
        @Environment(\.isEnabled) private var isEnabled
        @State private var isHovered = false
        var body: some View {
            configuration.label
                .background(Color.white.opacity(isEnabled ? (configuration.isPressed ? 0.12 : isHovered ? 0.06 : 0) : 0),
                            in: RoundedRectangle(cornerRadius: 4))
                .opacity(isEnabled ? 1 : 0.4)
                .onHover { isHovered = $0 }
        }
    }
}

struct ActionIcon: View {
    let symbol: String
    var body: some View {
        Image(systemName: symbol).font(.system(size: AppTheme.iconSize))
            .frame(width: 28, height: 28).contentShape(Rectangle())
    }
}

struct StateDot: View {
    let box: Workspace
    var body: some View {
        Circle().fill(box.isRunning ? AppTheme.running : AppTheme.stopped)
            .frame(width: 7, height: 7).accessibilityHidden(true)
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
            }.buttonStyle(.bordered).controlSize(.large).tint(AppTheme.primaryButton)
        }.padding(24).frame(width: width)
            .background(AppTheme.background).tint(AppTheme.accent).preferredColorScheme(.dark)
            .onExitCommand { dismiss() }
    }
}
