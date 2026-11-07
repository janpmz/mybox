import AppKit

// Resolution-independent native artwork; no generated bitmap dependency.
let directory = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
for (points, scale) in [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)] {
    let pixels = points * scale
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
                                  bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                  colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    let context = NSGraphicsContext(bitmapImageRep: bitmap)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    context.cgContext.scaleBy(x: CGFloat(pixels) / 1024, y: CGFloat(pixels) / 1024)
    let bounds = NSRect(x: 70, y: 70, width: 884, height: 884)
    let shape = NSBezierPath(roundedRect: bounds, xRadius: 205, yRadius: 205)
    NSGradient(starting: NSColor(white: 0.26, alpha: 1), ending: NSColor(white: 0.14, alpha: 1))!.draw(in: shape, angle: -55)
    shape.addClip()
    let cube = NSBezierPath()
    cube.move(to: NSPoint(x: 512, y: 780)); cube.line(to: NSPoint(x: 765, y: 646)); cube.line(to: NSPoint(x: 765, y: 378))
    cube.line(to: NSPoint(x: 512, y: 244)); cube.line(to: NSPoint(x: 259, y: 378)); cube.line(to: NSPoint(x: 259, y: 646)); cube.close()
    cube.move(to: NSPoint(x: 259, y: 646)); cube.line(to: NSPoint(x: 512, y: 512)); cube.line(to: NSPoint(x: 765, y: 646))
    cube.move(to: NSPoint(x: 512, y: 512)); cube.line(to: NSPoint(x: 512, y: 244))
    cube.move(to: NSPoint(x: 386, y: 713)); cube.line(to: NSPoint(x: 639, y: 579))
    cube.lineWidth = 18; cube.lineCapStyle = .round; cube.lineJoinStyle = .round
    NSColor(white: 0.91, alpha: 1).setStroke(); cube.stroke()
    NSGraphicsContext.restoreGraphicsState()
    let suffix = scale == 2 ? "@2x" : ""
    try bitmap.representation(using: .png, properties: [:])!.write(to: directory.appendingPathComponent("icon_\(points)x\(points)\(suffix).png"))
}
