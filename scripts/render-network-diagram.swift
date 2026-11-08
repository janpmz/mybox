import AppKit

// Render the README network diagram without browser controls or dependencies.
// Run: swift -module-cache-path .build/module-cache scripts/render-network-diagram.swift
let output = CommandLine.arguments.dropFirst().first ?? "docs/diagrams/mybox-network.png"
let width: CGFloat = 1180
let height: CGFloat = 280
let scale: CGFloat = 2
let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(width * scale),
                              pixelsHigh: Int(height * scale), bitsPerSample: 8,
                              samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                              colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
let context = NSGraphicsContext(bitmapImageRep: bitmap)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = context
context.cgContext.scaleBy(x: scale, y: scale)

func color(_ red: CGFloat, _ green: CGFloat, _ blue: CGFloat) -> NSColor {
    NSColor(red: red / 255, green: green / 255, blue: blue / 255, alpha: 1)
}

color(13, 17, 23).setFill()
NSBezierPath(rect: NSRect(x: 0, y: 0, width: width, height: height)).fill()

func label(_ text: String, center: NSPoint, size: CGFloat = 18,
           weight: NSFont.Weight = .medium, foreground: NSColor = color(230, 237, 243)) {
    let attributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: size, weight: weight), .foregroundColor: foreground
    ]
    let dimensions = (text as NSString).size(withAttributes: attributes)
    (text as NSString).draw(at: NSPoint(x: center.x - dimensions.width / 2,
                                       y: center.y - dimensions.height / 2), withAttributes: attributes)
}

func box(_ text: String, x: CGFloat, y: CGFloat, width: CGFloat, height: CGFloat = 70,
         subtitle: String? = nil) {
    let rect = NSRect(x: x, y: y, width: width, height: height)
    let outline = NSBezierPath(roundedRect: rect, xRadius: 10, yRadius: 10)
    color(22, 27, 34).setFill()
    outline.fill()
    color(68, 80, 94).setStroke()
    outline.lineWidth = 1.5
    outline.stroke()
    label(text, center: NSPoint(x: rect.midX, y: rect.midY + (subtitle == nil ? 0 : 14)))
    if let subtitle {
        label(subtitle, center: NSPoint(x: rect.midX, y: rect.midY - 14), size: 16,
              weight: .regular, foreground: color(167, 181, 196))
    }
}

func arrow(_ points: [NSPoint]) {
    let path = NSBezierPath()
    path.move(to: points[0])
    for point in points.dropFirst() { path.line(to: point) }
    color(137, 182, 232).setStroke()
    path.lineWidth = 2
    path.lineJoinStyle = .round
    path.stroke()
    let tip = points.last!
    let head = NSBezierPath()
    head.move(to: tip)
    head.line(to: NSPoint(x: tip.x - 8, y: tip.y + 5))
    head.line(to: NSPoint(x: tip.x - 8, y: tip.y - 5))
    head.close()
    color(137, 182, 232).setFill()
    head.fill()
}

box("Linux box A", x: 40, y: 170, width: 160)
box("Linux box B", x: 40, y: 40, width: 160)
box("Mac bridge A", x: 245, y: 170, width: 160)
box("Mac bridge B", x: 245, y: 40, width: 160)
box("Mac firewall", x: 470, y: 80, width: 230, height: 120, subtitle: "and internet sharing")
box("Wi-Fi / Ethernet", x: 765, y: 105, width: 175)
box("Internet", x: 995, y: 105, width: 145)

arrow([NSPoint(x: 208, y: 205), NSPoint(x: 237, y: 205)])
arrow([NSPoint(x: 208, y: 75), NSPoint(x: 237, y: 75)])
arrow([NSPoint(x: 413, y: 205), NSPoint(x: 437, y: 205), NSPoint(x: 437, y: 168), NSPoint(x: 462, y: 168)])
arrow([NSPoint(x: 413, y: 75), NSPoint(x: 437, y: 75), NSPoint(x: 437, y: 112), NSPoint(x: 462, y: 112)])
arrow([NSPoint(x: 708, y: 140), NSPoint(x: 757, y: 140)])
arrow([NSPoint(x: 948, y: 140), NSPoint(x: 987, y: 140)])

NSGraphicsContext.restoreGraphicsState()
try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: output))
print("Rendered \(output)")
