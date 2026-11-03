import Foundation
import ApplicationServices
import AppKit

// One-shot, permission-gated extraction from the frontmost application's focused element.
// Do not force the OS's accessibility permission dialog in background.
guard AXIsProcessTrusted() else {
    fputs("Accessibility permission missing: authorize the launcher in System Settings.\n", stderr)
    exit(2)
}

guard let app = NSWorkspace.shared.frontmostApplication else {
    fputs("No foreground app\n", stderr)
    exit(3)
}
let appName = (app.localizedName ?? "").lowercased()
// Ignore our own GUI so observations cannot loop over generated answers.
if appName.contains("context-lens") || appName.contains("contextlens") {
    fputs("ContextLens is foreground; switch to an authorized practice window.\n", stderr)
    exit(4)
}
let element = AXUIElementCreateApplication(app.processIdentifier)

func attribute(_ element: AXUIElement, _ name: CFString) -> AnyObject? {
    var value: CFTypeRef?
    let result = AXUIElementCopyAttributeValue(element, name, &value)
    return result == .success ? value : nil
}

var visited = Set<UInt>()
var chunks = [String]()
let maxNodes = 250
let maxChars = 60000

func scan(_ node: AXUIElement, depth: Int) {
    if depth > 8 || visited.count >= maxNodes || chunks.joined(separator: "\n").count >= maxChars { return }
    let identity = UInt(bitPattern: Unmanaged.passUnretained(node).toOpaque())
    if visited.contains(identity) { return }
    visited.insert(identity)
    // Secure/password text fields must never be extracted.
    let role = attribute(node, kAXRoleAttribute as CFString) as? String ?? ""
    let subrole = attribute(node, kAXSubroleAttribute as CFString) as? String ?? ""
    if role.localizedCaseInsensitiveContains("secure") || subrole.localizedCaseInsensitiveContains("secure") {
        return
    }
    if let protected = attribute(node, "AXProtectedContent" as CFString) as? Bool, protected {
        return
    }

    for name in [kAXValueAttribute, kAXDescriptionAttribute, kAXTitleAttribute] {
        if let value = attribute(node, name as CFString) as? String {
            let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty && text.count < 30000 && !chunks.contains(text) {
                chunks.append(text)
            }
        }
    }
    if let children = attribute(node, kAXChildrenAttribute as CFString) as? [AXUIElement] {
        for child in children.prefix(80) { scan(child, depth: depth + 1) }
    }
}

if let focused = attribute(element, kAXFocusedUIElementAttribute as CFString) {
    scan(focused as! AXUIElement, depth: 0)
}
if chunks.isEmpty {
    scan(element, depth: 0)
}
print(chunks.joined(separator: "\n"))
