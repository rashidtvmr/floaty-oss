import Foundation
import ApplicationServices
import AppKit

// One-shot, permission-gated extraction from the frontmost application.
//
// Two output modes:
//   --mode text   legacy plain-text blob (byte-for-byte compatible)
//   --mode tree   one bounded JSON object, schema 1, source "ax"
//                 (see docs/A11Y-TREE-SHORTCUTS.md section 1.2)
//
// Do not force the OS's accessibility permission dialog in background.

// Defaults are sized for real web/app windows, not just native toolbars. A
// browser window nests the page content (AXWebArea) about eight levels below the
// window, and the question itself another five or six levels below that, so the
// old depth-8 default returned only the browser chrome. Measured on a Chrome
// practice-test page: the question sat at depth 13 and its options at depth 14,
// the window held ~355 nodes, and the rendered outline was ~12k characters.
// These bounds keep that content while still capping hostile or runaway trees.
struct Limits {
    var mode: String = "tree"
    var maxNodes: Int = 800
    var maxDepth: Int = 20
    var maxChars: Int = 32000
}

func parseArguments(_ arguments: [String]) -> Limits {
    var limits = Limits()
    var index = 1
    while index < arguments.count {
        let argument = arguments[index]
        func nextValue() -> String? {
            if index + 1 < arguments.count {
                index += 1
                return arguments[index]
            }
            return nil
        }
        switch argument {
        case "--mode":
            if let value = nextValue(), value == "text" || value == "tree" {
                limits.mode = value
            }
        case "--max-nodes":
            if let value = nextValue(), let number = Int(value) { limits.maxNodes = max(0, number) }
        case "--max-depth":
            if let value = nextValue(), let number = Int(value) { limits.maxDepth = max(0, number) }
        case "--max-chars":
            if let value = nextValue(), let number = Int(value) { limits.maxChars = max(0, number) }
        default:
            break
        }
        index += 1
    }
    return limits
}

func attribute(_ element: AXUIElement, _ name: CFString) -> AnyObject? {
    var value: CFTypeRef?
    let result = AXUIElementCopyAttributeValue(element, name, &value)
    return result == .success ? value : nil
}

func stringAttribute(_ element: AXUIElement, _ name: CFString) -> String? {
    guard let value = attribute(element, name) as? String else { return nil }
    return value
}

func boolAttribute(_ element: AXUIElement, _ name: CFString) -> Bool {
    if let value = attribute(element, name) as? Bool { return value }
    if let value = attribute(element, name) as? NSNumber { return value.boolValue }
    return false
}

func pointAttribute(_ element: AXUIElement, _ name: CFString) -> CGPoint? {
    guard let raw = attribute(element, name), CFGetTypeID(raw) == AXValueGetTypeID() else {
        return nil
    }
    let value = raw as! AXValue
    var point = CGPoint.zero
    guard AXValueGetType(value) == .cgPoint, AXValueGetValue(value, .cgPoint, &point) else {
        return nil
    }
    return point
}

func sizeAttribute(_ element: AXUIElement, _ name: CFString) -> CGSize? {
    guard let raw = attribute(element, name), CFGetTypeID(raw) == AXValueGetTypeID() else {
        return nil
    }
    let value = raw as! AXValue
    var size = CGSize.zero
    guard AXValueGetType(value) == .cgSize, AXValueGetValue(value, .cgSize, &size) else {
        return nil
    }
    return size
}

func normalizedRole(_ raw: String) -> String {
    var role = raw
    if role.hasPrefix("AX") { role = String(role.dropFirst(2)) }
    return role.lowercased()
}

func roleIsImage(_ role: String) -> Bool {
    let tokens = ["image", "canvas", "graphic", "icon", "chart", "drawing", "figure"]
    return tokens.contains { role.contains($0) }
}

func roleIsProtected(_ role: String) -> Bool {
    role.contains("secure") || role.contains("password")
}

func nodeBounds(_ node: AXUIElement) -> [Int]? {
    guard let position = pointAttribute(node, kAXPositionAttribute as CFString),
          let size = sizeAttribute(node, kAXSizeAttribute as CFString) else {
        return nil
    }
    func integer(_ value: CGFloat) -> Int? {
        let rounded = value.rounded()
        guard rounded.isFinite,
              rounded >= CGFloat(Int.min),
              rounded < CGFloat(Int.max) else {
            return nil
        }
        return Int(rounded)
    }
    guard let x = integer(position.x), let y = integer(position.y),
          let width = integer(size.width), let height = integer(size.height) else {
        return nil
    }
    if width <= 0 || height <= 0 { return nil }
    return [x, y, width, height]
}

func elementAttribute(_ element: AXUIElement, _ name: CFString) -> AXUIElement? {
    guard let value = attribute(element, name), CFGetTypeID(value) == AXUIElementGetTypeID() else {
        return nil
    }
    return (value as! AXUIElement)
}

func buildTree(_ root: AXUIElement, focusedElement: AXUIElement?, appName: String, pid: Int, limits: Limits) -> [String: Any] {
    var nodes = [[String: Any]]()
    var parentIds = [Int?]()
    var firstFocusedElementId: Int?
    var firstFocusedAttributeId: Int?
    var firstFocusedAttributeDepth = -1
    // AX returns autoreleased wrapper objects whose heap addresses are recycled
    // between fetches, so pointer identity is not a stable element identity: it
    // wrongly deduplicates distinct elements and can prune whole subtrees (for
    // example Chrome's AXWebArea, which sits behind several groups). Compare the
    // underlying elements with CFEqual instead.
    var visited = [AXUIElement]()
    var truncated = appName.count > limits.maxChars
    var charCount = 0

    func consume(_ value: String) -> String {
        let remaining = max(0, limits.maxChars - charCount)
        let bounded = String(value.prefix(remaining))
        if bounded.count < value.count { truncated = true }
        charCount += bounded.count
        return bounded
    }

    func walk(_ node: AXUIElement, parent: Int?, depth: Int) {
        if depth > limits.maxDepth || nodes.count >= limits.maxNodes {
            truncated = true
            return
        }
        if visited.contains(where: { CFEqual($0, node) }) { return }
        visited.append(node)

        let role = normalizedRole(stringAttribute(node, kAXRoleAttribute as CFString) ?? "")
        // The global menu bar is never practice content and would otherwise
        // consume the node budget before the app's window is reached.
        if role == "menubar" { return }
        let protected = roleIsProtected(role) || boolAttribute(node, "AXProtectedContent" as CFString)
        let rawName = stringAttribute(node, kAXTitleAttribute as CFString)
            ?? stringAttribute(node, kAXDescriptionAttribute as CFString)
            ?? ""
        let name = consume(rawName)
        let text = consume(protected ? "" : (stringAttribute(node, kAXValueAttribute as CFString) ?? ""))

        let id = nodes.count + 1
        let matchesFocusedElement = focusedElement.map { CFEqual(node, $0) } ?? false
        let focusedAttribute = boolAttribute(node, kAXFocusedAttribute as CFString)
        let focused = matchesFocusedElement || focusedAttribute
        nodes.append([
            "id": id,
            "parent": parent.map { $0 as Any } ?? NSNull(),
            "role": role,
            "name": name,
            "text": text,
            "focused": focused,
            "bounds": nodeBounds(node).map { $0 as Any } ?? NSNull(),
            "image": roleIsImage(role),
            "protected": protected,
        ])
        parentIds.append(parent)
        if matchesFocusedElement && firstFocusedElementId == nil {
            firstFocusedElementId = id
        }
        if focusedAttribute && depth > firstFocusedAttributeDepth {
            firstFocusedAttributeId = id
            firstFocusedAttributeDepth = depth
        }

        if let children = attribute(node, kAXChildrenAttribute as CFString) as? [AXUIElement] {
            if children.count > 80 { truncated = true }
            for child in children.prefix(80) {
                if nodes.count >= limits.maxNodes {
                    truncated = true
                    break
                }
                walk(child, parent: id, depth: depth + 1)
            }
        }
    }

    walk(root, parent: nil, depth: 0)

    // The focused node and its ancestors are marked focused.
    var focusedSet = Set<Int>()
    if let focusedId = firstFocusedElementId ?? firstFocusedAttributeId {
        focusedSet.insert(focusedId)
        var current = parentIds[focusedId - 1]
        while let parent = current {
            focusedSet.insert(parent)
            current = parentIds[parent - 1]
        }
    }
    for index in nodes.indices {
        nodes[index]["focused"] = focusedSet.contains(index + 1)
    }

    return [
        "schema": 1,
        "source": "ax",
        "app": String(appName.prefix(limits.maxChars)),
        "pid": pid,
        "truncated": truncated,
        "node_count": nodes.count,
        "nodes": nodes,
    ]
}

func runMacOSAccessibility(_ arguments: [String]) -> Int32 {
    let limits = parseArguments(arguments)

    guard AXIsProcessTrusted() else {
        fputs("Accessibility permission missing: authorize SocketStream in System Settings.\n", stderr)
        return 2
    }

    guard let app = NSWorkspace.shared.frontmostApplication else {
        fputs("No foreground app\n", stderr)
        return 3
    }
    let appName = (app.localizedName ?? "").lowercased()
    // Ignore our own GUI so observations cannot loop over generated answers.
    if appName.contains("socket_stream") || appName.contains("socket_stream") {
        fputs("SocketStream is foreground; switch to an authorized practice window.\n", stderr)
        return 4
    }
    let appElement = AXUIElementCreateApplication(app.processIdentifier)
    // Prefer the focused window. Rooting at the application element would put the
    // global menu bar first and starve the window content of the node budget.
    let focusedWindow = elementAttribute(appElement, kAXFocusedWindowAttribute as CFString)
        ?? elementAttribute(appElement, kAXMainWindowAttribute as CFString)
        ?? (attribute(appElement, kAXWindowsAttribute as CFString) as? [AXUIElement])?.first
    let element = focusedWindow ?? appElement
    let focusedElement = elementAttribute(appElement, kAXFocusedUIElementAttribute as CFString)

    if limits.mode == "tree" {
        let payload = buildTree(
            element,
            focusedElement: focusedElement,
            appName: app.localizedName ?? "",
            pid: Int(app.processIdentifier),
            limits: limits
        )
        if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
           let text = String(data: data, encoding: .utf8) {
            print(text)
            return 0
        }
        fputs("Accessibility tree could not be serialized.\n", stderr)
        return 3
    }

    // Legacy text mode: unchanged behaviour.
    // Same CFEqual-based dedup as tree mode: pointer identity is unstable here.
    // Depth matches the tree defaults so page content below the web area is
    // reached instead of stopping at the browser chrome.
    var visited = [AXUIElement]()
    var chunks = [String]()
    let maxNodes = 800
    let maxChars = 60000

    func scan(_ node: AXUIElement, depth: Int) {
        if depth > 20 || visited.count >= maxNodes || chunks.joined(separator: "\n").count >= maxChars { return }
        if visited.contains(where: { CFEqual($0, node) }) { return }
        visited.append(node)
        // Secure/password text fields must never be extracted.
        let role = attribute(node, kAXRoleAttribute as CFString) as? String ?? ""
        let subrole = attribute(node, kAXSubroleAttribute as CFString) as? String ?? ""
        if role.localizedCaseInsensitiveContains("menubar") { return }
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

    if let focused = focusedElement {
        scan(focused, depth: 0)
    }
    if chunks.isEmpty {
        scan(element, depth: 0)
    }
    print(chunks.joined(separator: "\n"))
    return 0
}

#if SOCKET_STREAM_ACCESSIBILITY_STANDALONE
exit(runMacOSAccessibility(CommandLine.arguments))
#endif
