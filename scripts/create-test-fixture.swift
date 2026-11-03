import AppKit
import Foundation

let width = 1000
let height = 260
guard let bitmap = NSBitmapImageRep(
    bitmapDataPlanes: nil,
    pixelsWide: width,
    pixelsHigh: height,
    bitsPerSample: 8,
    samplesPerPixel: 4,
    hasAlpha: true,
    isPlanar: false,
    colorSpaceName: .deviceRGB,
    bytesPerRow: 0,
    bitsPerPixel: 0
) else { exit(1) }
guard let ctx = NSGraphicsContext(bitmapImageRep: bitmap) else { exit(1) }
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = ctx
NSColor.white.setFill()
NSRect(x: 0, y: 0, width: width, height: height).fill()

let attr: [NSAttributedString.Key: Any] = [
    .font: NSFont.monospacedSystemFont(ofSize: 34, weight: .regular),
    .foregroundColor: NSColor.black
]
("function twoSum(nums, target) {" as NSString).draw(at: NSPoint(x: 45, y: 175), withAttributes: attr)
("  return nums[0] + nums[1] == target;" as NSString).draw(at: NSPoint(x: 45, y: 112), withAttributes: attr)
("}" as NSString).draw(at: NSPoint(x: 45, y: 49), withAttributes: attr)
ctx.flushGraphics()
NSGraphicsContext.restoreGraphicsState()
guard let png = bitmap.representation(using: .png, properties: [:]) else { exit(1) }
try png.write(to: URL(fileURLWithPath: "tests/fixtures/sample-code.png"))
print("Fixture written: tests/fixtures/sample-code.png")
