import AppKit
import Foundation
import UniformTypeIdentifiers
import ApplicationServices
import CoreGraphics

/// Stable action names, in the order used by the docs, the config file and the
/// Rust core (see docs/A11Y-TREE-SHORTCUTS.md section 3).
let shortcutActions = [
    "start", "stop", "pause_resume", "toggle_overlay", "show_overlay", "hide_overlay",
    "copy_answer", "compact_toggle", "toggle_stealth", "open_settings", "refresh_question", "quit",
]

/// Default accelerator per action; must match shortcuts.rs and shortcuts.py.
let defaultShortcutBindings: [String: String] = [
    "start": "ctrl+alt+s",
    "stop": "ctrl+alt+x",
    "pause_resume": "ctrl+alt+p",
    "toggle_overlay": "ctrl+alt+o",
    "show_overlay": "ctrl+alt+shift+o",
    "hide_overlay": "ctrl+alt+h",
    "copy_answer": "ctrl+alt+c",
    "compact_toggle": "ctrl+alt+m",
    "toggle_stealth": "ctrl+alt+k",
    "open_settings": "ctrl+alt+comma",
    "refresh_question": "ctrl+alt+r",
    "quit": "ctrl+alt+q",
]

/// One parsed accelerator: modifiers plus exactly one key. Mirrors the Rust and
/// Python grammar so the config file means the same thing on every platform.
struct Accelerator: Equatable {
    var ctrl = false
    var alt = false
    var shift = false
    var meta = false
    var key = ""

    private static let modifierAliases: [String: String] = [
        "ctrl": "ctrl", "control": "ctrl",
        "alt": "alt", "opt": "alt", "option": "alt",
        "shift": "shift",
        "meta": "meta", "cmd": "meta", "super": "meta", "win": "meta",
    ]

    private static let namedKeys: Set<String> = [
        "space", "comma", "period", "slash", "semicolon", "apostrophe",
        "minus", "equals", "tab", "enter", "escape", "backspace",
    ]

    private static func isSingleLetterOrDigit(_ text: String) -> Bool {
        text.count == 1 && text.range(of: "^[a-z0-9]$", options: .regularExpression) != nil
    }

    private static func validKey(_ key: String) -> Bool {
        if isSingleLetterOrDigit(key) { return true }
        if namedKeys.contains(key) { return true }
        if key.hasPrefix("f"), let number = Int(key.dropFirst()) {
            return (1...24).contains(number)
        }
        return false
    }

    static func parse(_ value: String) -> Accelerator? {
        if value.trimmingCharacters(in: .whitespaces).isEmpty { return nil }
        var accel = Accelerator()
        var key = ""
        for raw in value.split(separator: "+", omittingEmptySubsequences: false) {
            let part = raw.trimmingCharacters(in: .whitespaces).lowercased()
            if part.isEmpty { return nil }
            if let name = modifierAliases[part] {
                switch name {
                case "ctrl":
                    if accel.ctrl { return nil }
                    accel.ctrl = true
                case "alt":
                    if accel.alt { return nil }
                    accel.alt = true
                case "shift":
                    if accel.shift { return nil }
                    accel.shift = true
                default:
                    if accel.meta { return nil }
                    accel.meta = true
                }
                continue
            }
            if !validKey(part) { return nil }
            if !key.isEmpty { return nil }
            key = part
        }
        if key.isEmpty { return nil }
        accel.key = key
        return accel
    }

    var display: String {
        var parts: [String] = []
        if ctrl { parts.append("Ctrl") }
        if alt { parts.append("Alt") }
        if shift { parts.append("Shift") }
        if meta { parts.append("Meta") }
        parts.append(Self.displayKey(key))
        return parts.joined(separator: "+")
    }

    private static func displayKey(_ key: String) -> String {
        if key.count == 1 { return key.uppercased() }
        if key.hasPrefix("f"), Int(key.dropFirst()) != nil { return key.uppercased() }
        return key.prefix(1).uppercased() + String(key.dropFirst())
    }

    /// The AppKit menu key equivalent for this accelerator's key.
    var menuKeyEquivalent: String {
        switch key {
        case "comma": return ","
        case "period": return "."
        case "slash": return "/"
        case "semicolon": return ";"
        case "apostrophe": return "'"
        case "minus": return "-"
        case "equals": return "="
        case "space": return " "
        case "tab": return "\t"
        case "enter": return "\r"
        case "escape": return "\u{1b}"
        case "backspace": return "\u{8}"
        default: return key
        }
    }

    var menuModifierMask: NSEvent.ModifierFlags {
        var mask: NSEvent.ModifierFlags = []
        if ctrl { mask.insert(.control) }
        if alt { mask.insert(.option) }
        if shift { mask.insert(.shift) }
        if meta { mask.insert(.command) }
        return mask
    }

    /// The key token for a global keyDown event, or nil when the key is not one
    /// the global monitor can match (function keys stay menu-only).
    static func keyToken(for characters: String) -> String? {
        if isSingleLetterOrDigit(characters.lowercased()) { return characters.lowercased() }
        switch characters {
        case ",": return "comma"
        case ".": return "period"
        case "/": return "slash"
        case ";": return "semicolon"
        case "'": return "apostrophe"
        case "-": return "minus"
        case "=": return "equals"
        case " ": return "space"
        default: return nil
        }
    }
}

// MARK: - Live observation state

/// The subset of the `observe-live` JSONL schema (schema 1) that the panel
/// renders. Mirrors apps/socket_stream_desktop/ui/live_state.py so both desktops
/// accept and reject exactly the same events.
struct LiveView {
    var status = "idle"
    var sessionID = ""
    var requestID = ""
    var question = ""
    /// Role-free form of `question` for display; falls back to `question`.
    var questionDisplay = ""
    var answer = ""
    var source = ""
    var error = ""
    /// Compact rendering of the provider request and response, shown in the
    /// panel's API area. Empty until the first exchange of the session.
    var apiRequest = ""
    var apiResponse = ""
    var transcript = ""
}

final class LiveState {
    private(set) var view = LiveView()
    private var sequence = -1

    private static let eventTypes: Set<String> = [
        "session_started", "question_detected", "answer_started", "answer_delta",
        "answer_completed", "api_request", "api_response", "transcript_updated",
        "session_paused", "session_resumed", "error", "session_stopped",
    ]
    private static let sources: Set<String> = [
        "system", "provider", "accessibility", "ocr",
    ]
    private static let readingSources: Set<String> = [
        "screen", "accessibility", "ocr", "microphone", "playback", "file",
    ]
    private static let ansi = try! NSRegularExpression(
        pattern: "\u{1b}(?:\\[[0-?]*[ -/]*[@-~]|\\][^\u{07}]*(?:\u{07}|\u{1b}\\\\))")

    /// Strip ANSI escapes and control characters, then bound the result.
    static func clean(_ value: Any?, limit: Int) -> String {
        guard limit > 0, let text = value as? String else { return "" }
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        let stripped = ansi.stringByReplacingMatches(in: text, options: [], range: range, withTemplate: "")
        var output = ""
        output.unicodeScalars.append(contentsOf: stripped.unicodeScalars.filter { scalar in
            !(scalar.value <= 0x08 || (scalar.value >= 0x0b && scalar.value <= 0x1f) || scalar.value == 0x7f)
        })
        return String(output.prefix(limit))
    }

    /// Forget the previous session before a new observation starts.
    func reset() {
        view = LiveView()
        sequence = -1
    }

    /// JSON booleans bridge to NSNumber on Darwin; reject them for integer fields.
    private static func intValue(_ any: Any?) -> Int? {
        guard let number = any as? NSNumber else { return nil }
        if CFGetTypeID(number) == CFBooleanGetTypeID() { return nil }
        return number.intValue
    }

    func apply(_ event: [String: Any]) -> Bool {
        guard Self.intValue(event["schema"]) == 1,
              let kind = event["type"] as? String, Self.eventTypes.contains(kind),
              let sequence = Self.intValue(event["sequence"]), sequence > self.sequence,
              let sessionID = event["session_id"] as? String,
              !sessionID.isEmpty, sessionID.count <= 128 else { return false }

        if kind == "session_started" {
            guard view.status == "idle" || view.status == "stopped" else { return false }
            self.sequence = sequence
            view = LiveView(status: "observing", sessionID: sessionID, requestID: "",
                            question: "", questionDisplay: "", answer: "", source: "", error: "")
            return true
        }
        if kind == "error" && view.status == "idle" {
            self.sequence = sequence
            let source = event["source"] as? String ?? "system"
            view.status = "error"
            view.sessionID = sessionID
            view.source = Self.sources.contains(source) ? source : "system"
            view.error = Self.clean(event["message"], limit: 500)
            return true
        }
        guard sessionID == view.sessionID, view.status != "idle", view.status != "stopped" else {
            return false
        }
        self.sequence = sequence

        if kind == "session_stopped" {
            view.status = "stopped"
            return true
        }
        if kind == "session_paused" {
            if view.status == "paused" { return false }
            view.status = "paused"
            return true
        }
        if kind == "session_resumed" {
            if view.status != "paused" { return false }
            view.status = "observing"
            view.error = ""
            return true
        }
        if view.status == "paused" { return false }
        if kind == "error" {
            view.status = "error"
            view.error = Self.clean(event["message"], limit: 500)
            return true
        }

        if kind == "transcript_updated" {
            view.transcript = Self.clean(event["text"], limit: 24000)
            return true
        }
        guard let requestID = event["request_id"] as? String,
              !requestID.isEmpty, requestID.count <= 128 else { return false }

        if kind == "question_detected" {
            let question = Self.clean(event["text"], limit: 12000).trimmingCharacters(in: .whitespacesAndNewlines)
            if question.isEmpty || requestID == view.requestID { return false }
            let display = Self.clean(event["display"], limit: 12000).trimmingCharacters(in: .whitespacesAndNewlines)
            let source = event["source"] as? String ?? "screen"
            view.status = "thinking"
            view.requestID = requestID
            view.question = question
            view.questionDisplay = display.isEmpty ? question : display
            view.answer = ""
            view.source = Self.readingSources.contains(source) ? source : "screen"
            view.error = ""
            view.apiRequest = ""
            view.apiResponse = ""
            return true
        }
        guard requestID == view.requestID else { return false }
        if kind == "answer_started" {
            if view.status == "thinking" { return false }
            view.status = "thinking"
            return true
        }
        if kind == "answer_delta" {
            if view.status == "ready" { return false }
            let room = 64000 - view.answer.count
            let addition = Self.clean(event["text"], limit: max(room, 0))
            if addition.isEmpty { return false }
            view.answer += addition
            view.status = "thinking"
            return true
        }
        if kind == "answer_completed" {
            view.status = "ready"
            return true
        }
        if kind == "api_request" {
            let url = Self.clean(event["url"], limit: 500)
            let model = Self.clean(event["model"], limit: 200)
            let prompt = Self.clean(event["prompt"], limit: 4000)
            view.apiRequest = "POST \(url)\nmodel: \(model)\n\n\(prompt)"
            return true
        }
        if kind == "api_response" {
            let status = Self.intValue(event["status"]).map { "HTTP \($0)" } ?? "local"
            let text = Self.clean(event["text"], limit: 4000)
            view.apiResponse = "\(status)\n\n\(text)"
            return true
        }
        return false
    }
}

// MARK: - Saved provider settings

/// Provider settings kept in this app's own preferences (`UserDefaults`), so a
/// configured custom endpoint, model and API key survive a quit and relaunch.
/// Nothing here is shared with any other app, and the key is never logged.
enum SavedSettings {
    private static let prefix = "socket_stream.provider."

    static let endpointKey = prefix + "endpoint"
    static let modelKey = prefix + "model"
    static let apiKeyKey = prefix + "apiKey"
    static let ollamaModelKey = prefix + "ollamaModel"
    static let codexModelKey = prefix + "codexModel"
    static let providerKey = prefix + "provider"
    static let rememberKey = prefix + "remember"

    /// Whether the user asked for settings to be remembered. Defaults to on so
    /// a configured provider keeps working after a restart.
    static func remember(defaults: UserDefaults = .standard) -> Bool {
        if defaults.object(forKey: rememberKey) == nil { return true }
        return defaults.bool(forKey: rememberKey)
    }

    static func load(into state: MainWindow, defaults: UserDefaults = .standard) {
        guard remember(defaults: defaults) else { return }
        if let value = defaults.string(forKey: endpointKey), !value.isEmpty {
            state.providerEndpoint = value
        }
        if let value = defaults.string(forKey: modelKey), !value.isEmpty {
            state.providerModel = value
        }
        if let value = defaults.string(forKey: apiKeyKey), !value.isEmpty {
            state.providerApiKey = value
        }
        if let value = defaults.string(forKey: ollamaModelKey), !value.isEmpty {
            state.ollamaModel = value
        }
        if let value = defaults.string(forKey: codexModelKey), !value.isEmpty {
            state.codexModel = value
        }
        // The provider selection is restored after the popup exists.
        if let value = defaults.string(forKey: providerKey), !value.isEmpty {
            state.pendingProvider = value
        }
    }

    static func save(_ state: MainWindow, defaults: UserDefaults = .standard) {
        guard remember(defaults: defaults) else {
            clear(defaults: defaults)
            return
        }
        // The endpoint and model are non-secret; store them even when empty so
        // clearing a field is itself remembered.
        defaults.set(state.providerEndpoint, forKey: endpointKey)
        defaults.set(state.providerModel, forKey: modelKey)
        defaults.set(state.ollamaModel, forKey: ollamaModelKey)
        defaults.set(state.codexModel, forKey: codexModelKey)
        // `providerName` falls back to the launch option when the popup has not
        // been built (panel-only mode), which is still the honest selection.
        defaults.set(state.providerName, forKey: providerKey)
        if state.providerApiKey.isEmpty {
            defaults.removeObject(forKey: apiKeyKey)
        } else {
            defaults.set(state.providerApiKey, forKey: apiKeyKey)
        }
    }

    static func clear(defaults: UserDefaults = .standard) {
        for key in [endpointKey, modelKey, apiKeyKey, ollamaModelKey, codexModelKey, providerKey] {
            defaults.removeObject(forKey: key)
        }
    }
}

/// Round-trips `SavedSettings` against a scratch preferences suite so the
/// persistence contract (and the remembered provider) is checked without
/// touching the real preferences.
enum SavedSettingsSelfTest {
    static func run() -> [String] {
        var failures: [String] = []
        func expect(_ condition: Bool, _ message: String) {
            if !condition { failures.append(message) }
        }
        let suiteName = "dev.socket_stream.selftest.\(ProcessInfo.processInfo.processIdentifier)"
        guard let scratch = UserDefaults(suiteName: suiteName) else {
            return ["scratch preferences suite unavailable"]
        }
        defer { scratch.removePersistentDomain(forName: suiteName) }

        let state = MainWindow(options: LaunchOptions())
        expect(SavedSettings.remember(defaults: scratch), "remember defaults to on")

        state.providerEndpoint = "https://api.example.invalid/v1"
        state.providerModel = "gpt-test-42"
        state.providerApiKey = "sk-secret-abc-123"
        state.ollamaModel = "qwen-test:7b"
        state.codexModel = "gpt-5-codex"
        state.pendingProvider = "codex"
        SavedSettings.save(state, defaults: scratch)

        let restored = MainWindow(options: LaunchOptions())
        SavedSettings.load(into: restored, defaults: scratch)
        expect(restored.providerEndpoint == state.providerEndpoint, "endpoint restored")
        expect(restored.providerModel == state.providerModel, "model restored")
        expect(restored.providerApiKey == state.providerApiKey, "api key restored")
        expect(restored.ollamaModel == state.ollamaModel, "ollama model restored")
        expect(restored.codexModel == state.codexModel, "codex model restored")
        expect(restored.pendingProvider == "codex", "provider selection restored")

        // Opting out clears the stored values and stops restoring them.
        scratch.set(false, forKey: SavedSettings.rememberKey)
        SavedSettings.save(state, defaults: scratch)
        let optedOut = MainWindow(options: LaunchOptions())
        SavedSettings.load(into: optedOut, defaults: scratch)
        expect(!SavedSettings.remember(defaults: scratch), "remember off is honoured")
        expect(optedOut.providerApiKey.isEmpty, "api key cleared when not remembered")
        expect(optedOut.pendingProvider == nil, "provider cleared when not remembered")
        return failures
    }
}

// MARK: - Launch options

struct LaunchOptions {
    var panelOnly = false
    var provider: String?
    var interval = 4
    var allowOCR = false
    var stealth = true
    var compact = false
    var opacity = 0.94
    var cli: String?

    static func parse(_ arguments: [String]) -> LaunchOptions {
        var options = LaunchOptions()
        var index = 1
        func nextValue() -> String? {
            if index + 1 < arguments.count {
                index += 1
                return arguments[index]
            }
            return nil
        }
        while index < arguments.count {
            let argument = arguments[index]
            switch argument {
            case "--panel-only":
                options.panelOnly = true
            case "--provider":
                options.provider = nextValue()
            case "--interval":
                if let value = nextValue(), let number = Int(value) {
                    options.interval = min(3600, max(2, number))
                }
            case "--allow-ocr":
                options.allowOCR = true
            case "--stealth":
                options.stealth = true
            case "--no-stealth":
                options.stealth = false
            case "--compact":
                options.compact = true
            case "--opacity":
                if let value = nextValue(), let number = Double(value), (0.85...1.0).contains(number) {
                    options.opacity = number
                }
            case "--cli":
                options.cli = nextValue()
            default:
                break
            }
            index += 1
        }
        return options
    }
}

// MARK: - Floating answer panel

/// A floating answer panel. macOS can honestly exclude it from screen capture
/// with `NSWindow.sharingType = .none` — a real, documented AppKit API, though
/// macOS may still include the window in some captures depending on the API and
/// OS version. It never hides on focus changes or when the main window is
/// minimized; only an explicit Close / Hide action changes visibility.
/// An `NSPanel` that can never become key or main. `.nonactivatingPanel` only
/// stops the *click* from activating the app; a view that accepts first
/// responder (the selectable answer text) still drives `makeKeyWindow` and
/// activates SocketStream. With `canBecomeKey = false` the panel stays
/// non-activating for every click, including drag-selection. What is lost is
/// keyboard input to the panel: no arrow/Page scrolling, no Cmd+C, no key
/// equivalents. The panel is mouse-driven and has its own Copy button, which
/// copies the selection when there is one.
final class NonActivatingAnswerPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

final class AnswerPanel: NSObject, NSWindowDelegate {
    let panel: NonActivatingAnswerPanel
    private let statusLabel: NSTextField
    private let questionLabel: NSTextField
    private let answerView: NSTextView
    private let answerScroll: NSScrollView
    private let apiView: NSTextView
    private let apiScroll: NSScrollView
    private let compactButton: NSButton
    private let pauseButton: NSButton
    private let copyButton: NSButton
    private let closeButton: NSButton
    private let mainButton: NSButton
    private let stopButton: NSButton
    private var controls: NSStackView!
    private var positioned = false
    private var apiExpandedHeight: NSLayoutConstraint!
    private var apiCollapsedHeight: NSLayoutConstraint!

    private(set) var compact = false
    private(set) var stealth = true
    private(set) var running = false
    /// Mirrors the last state applied to the widgets so a stream of deltas does
    /// not rebuild the whole text view on every event.
    private var lastQuestion = ""
    private var lastAnswer = ""
    private var lastAPI = ""

    var onStop: (() -> Void)?
    var onPause: (() -> Void)?
    var onCopy: (() -> Void)?
    var onShowMain: (() -> Void)?

    init(stealth: Bool = true, compact: Bool = false, opacity: Double = 0.94) {
        let frame = NSRect(x: 0, y: 0, width: 520, height: 430)
        panel = NonActivatingAnswerPanel(contentRect: frame,
                        styleMask: [.titled, .closable, .utilityWindow, .nonactivatingPanel, .resizable],
                        backing: .buffered, defer: false)
        panel.title = "SocketStream — Practice Answer"
        panel.isFloatingPanel = true
        // The status-bar level is high enough to remain above fullscreen apps
        // while `.fullScreenAuxiliary` lets the panel join their space.
        panel.level = .statusBar
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        panel.isReleasedWhenClosed = false
        panel.isMovableByWindowBackground = true
        panel.becomesKeyOnlyIfNeeded = true
        panel.alphaValue = min(1.0, max(0.85, opacity))
        panel.minSize = NSSize(width: 340, height: 210)
        panel.standardWindowButton(.miniaturizeButton)?.isHidden = true
        panel.standardWindowButton(.zoomButton)?.isHidden = true
        panel.setAccessibilityIdentifier("socket_stream_panel")
        // Keep the user's chosen spot across launches; `show()` only centers a
        // panel that has never been placed.
        panel.setFrameAutosaveName("SocketStreamAnswerPanel")

        statusLabel = NSTextField(labelWithString: "Waiting for a practice question")
        statusLabel.font = NSFont.systemFont(ofSize: 11, weight: .semibold)
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.lineBreakMode = .byTruncatingTail
        statusLabel.setAccessibilityIdentifier("panel-status")

        questionLabel = NSTextField(wrappingLabelWithString: "No question detected yet")
        questionLabel.font = NSFont.systemFont(ofSize: 13, weight: .medium)
        questionLabel.maximumNumberOfLines = 4
        questionLabel.lineBreakMode = .byTruncatingTail
        questionLabel.setContentCompressionResistancePriority(.defaultLow, for: .vertical)
        questionLabel.setAccessibilityIdentifier("panel-question")

        answerView = NSTextView(frame: NSRect(x: 0, y: 0, width: 480, height: 200))
        answerView.isEditable = false
        answerView.isSelectable = true
        answerView.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        answerView.textContainerInset = NSSize(width: 4, height: 4)
        answerView.string = "The model answer appears here."
        answerView.setAccessibilityIdentifier("panel-answer")

        answerScroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 480, height: 200))
        answerScroll.hasVerticalScroller = true
        answerScroll.autohidesScrollers = true
        answerScroll.borderType = .bezelBorder
        answerScroll.documentView = answerView

        // A small, read-only view of the provider exchange: the endpoint,
        // model and prompt preview that were sent, and the response that came
        // back. It never contains the API key.
        apiView = NSTextView(frame: NSRect(x: 0, y: 0, width: 480, height: 64))
        apiView.isEditable = false
        apiView.isSelectable = true
        apiView.font = NSFont.monospacedSystemFont(ofSize: 10, weight: .regular)
        apiView.textContainerInset = NSSize(width: 4, height: 4)
        apiView.string = "API request and response appear here."
        apiView.setAccessibilityIdentifier("panel-api")

        apiScroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 480, height: 64))
        apiScroll.hasVerticalScroller = true
        apiScroll.autohidesScrollers = true
        apiScroll.borderType = .bezelBorder
        apiScroll.documentView = apiView

        compactButton = AnswerPanel.makeButton("Compact", action: nil, tooltip: "Toggle the compact panel layout")
        pauseButton = AnswerPanel.makeButton("Pause", action: nil, tooltip: "Pause or resume screen observation")
        copyButton = AnswerPanel.makeButton("Copy", action: nil, tooltip: "Copy the model answer to the clipboard")
        closeButton = AnswerPanel.makeButton("Close", action: nil, tooltip: "Hide the panel; the practice session keeps running")
        mainButton = AnswerPanel.makeButton("Main", action: nil, tooltip: "Bring the SocketStream main window forward")
        stopButton = AnswerPanel.makeButton("Stop practice", action: nil, tooltip: "Stop the observation session")
        stopButton.bezelStyle = .rounded

        super.init()

        panel.delegate = self
        buildLayout()
        compactButton.target = self
        compactButton.action = #selector(toggleCompact)
        pauseButton.target = self
        pauseButton.action = #selector(togglePause)
        copyButton.target = self
        copyButton.action = #selector(copyAnswerAction)
        closeButton.target = self
        closeButton.action = #selector(hidePanel)
        mainButton.target = self
        mainButton.action = #selector(showMain)
        stopButton.target = self
        stopButton.action = #selector(stopPractice)
        setStealth(stealth)
        setCompact(compact)
        setRunning(false)
    }

    private static func makeButton(_ title: String, action: Selector?, tooltip: String) -> NSButton {
        let button = NSButton(title: title, target: nil, action: action)
        button.bezelStyle = .texturedRounded
        button.controlSize = .small
        button.font = NSFont.systemFont(ofSize: 11)
        button.toolTip = tooltip
        return button
    }

    private func buildLayout() {
        let content = NSView()
        content.wantsLayer = true
        // Utility panels pick up a vibrant translucent material; a near-opaque
        // backing keeps the answer legible over busy practice content while the
        // window's 94% alpha still makes it feel light.
        let background = NSColor.windowBackgroundColor.withAlphaComponent(0.97)
        content.layer?.backgroundColor = background.cgColor
        panel.backgroundColor = background
        panel.contentView = content

        for view in [statusLabel, questionLabel, answerScroll, apiScroll, compactButton, pauseButton,
                     copyButton, closeButton, mainButton, stopButton] {
            view.translatesAutoresizingMaskIntoConstraints = false
        }

        controls = NSStackView(views: [compactButton, pauseButton, copyButton, closeButton, mainButton, stopButton])
        controls.orientation = .horizontal
        controls.alignment = .centerY
        controls.spacing = 6
        controls.setHuggingPriority(.defaultLow, for: .horizontal)
        controls.translatesAutoresizingMaskIntoConstraints = false

        content.addSubview(statusLabel)
        content.addSubview(questionLabel)
        content.addSubview(answerScroll)
        content.addSubview(apiScroll)
        content.addSubview(controls)

        NSLayoutConstraint.activate([
            statusLabel.topAnchor.constraint(equalTo: content.topAnchor, constant: 10),
            statusLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 14),
            statusLabel.trailingAnchor.constraint(lessThanOrEqualTo: content.trailingAnchor, constant: -14),

            questionLabel.topAnchor.constraint(equalTo: statusLabel.bottomAnchor, constant: 5),
            questionLabel.leadingAnchor.constraint(equalTo: statusLabel.leadingAnchor),
            questionLabel.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -14),
            questionLabel.heightAnchor.constraint(lessThanOrEqualToConstant: 62),

            answerScroll.topAnchor.constraint(equalTo: questionLabel.bottomAnchor, constant: 7),
            answerScroll.leadingAnchor.constraint(equalTo: statusLabel.leadingAnchor),
            answerScroll.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -14),
            answerScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 80),

            apiScroll.topAnchor.constraint(equalTo: answerScroll.bottomAnchor, constant: 7),
            apiScroll.leadingAnchor.constraint(equalTo: statusLabel.leadingAnchor),
            apiScroll.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -14),

            controls.topAnchor.constraint(equalTo: apiScroll.bottomAnchor, constant: 8),
            controls.leadingAnchor.constraint(equalTo: statusLabel.leadingAnchor),
            controls.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -14),
            controls.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -10),
            controls.heightAnchor.constraint(equalToConstant: 24),
        ])

        // The API area is a fixed-height strip that the compact layout collapses
        // to nothing. A lower priority lets the window shrink gracefully.
        apiExpandedHeight = apiScroll.heightAnchor.constraint(equalToConstant: 64)
        apiExpandedHeight.priority = .defaultHigh
        apiCollapsedHeight = apiScroll.heightAnchor.constraint(equalToConstant: 0)
        apiExpandedHeight.isActive = true
    }

    // ---- visibility ------------------------------------------------------

    private func centerOnScreen() {
        guard let screen = panel.screen ?? NSScreen.main else { return }
        let visible = screen.visibleFrame
        let size = panel.frame.size
        let origin = NSPoint(x: visible.midX - size.width / 2,
                             y: visible.midY - size.height / 2)
        panel.setFrameOrigin(origin)
    }

    func show() {
        if !positioned {
            let onScreen = NSScreen.screens.contains { $0.visibleFrame.intersects(panel.frame) }
            if panel.frame.origin == .zero || !onScreen {
                centerOnScreen()
            }
            positioned = true
        }
        // `orderFrontRegardless` shows the panel without activating the app or
        // stealing focus from the practice window.
        panel.orderFrontRegardless()
    }

    func hide() {
        panel.orderOut(nil)
    }

    func toggle() {
        if panel.isVisible { hide() } else { show() }
    }

    var isVisible: Bool { panel.isVisible }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        hide()
        return false
    }

    // ---- content ---------------------------------------------------------

    func setStatus(_ text: String, error: Bool = false) {
        statusLabel.stringValue = text.isEmpty ? " " : text
        statusLabel.textColor = error ? .systemRed : .secondaryLabelColor
    }

    func setQuestion(_ text: String) {
        let value = text.isEmpty ? "No question detected yet" : String(text.prefix(650))
        if value != lastQuestion {
            lastQuestion = value
            questionLabel.stringValue = value
        }
    }

    func setAnswer(_ text: String) {
        guard text != lastAnswer else { return }
        lastAnswer = text
        answerView.string = text
        if !text.isEmpty {
            answerView.scrollToEndOfDocument(nil)
        }
    }

    /// Render the provider exchange in the small API area. Empty strings show a
    /// placeholder so the area never looks broken before the first call.
    func setAPI(_ request: String, response: String) {
        var parts: [String] = []
        if !request.isEmpty { parts.append("Request\n\(request)") }
        if !response.isEmpty { parts.append("Response\n\(response)") }
        let value = parts.isEmpty ? "API request and response appear here." : parts.joined(separator: "\n\n──────\n")
        guard value != lastAPI else { return }
        lastAPI = value
        apiView.string = value
        apiView.scrollToBeginningOfDocument(nil)
    }

    func copyAnswer() {
        onCopy?()
    }

    var answerText: String { answerView.string }

    /// The drag-selected part of the answer, or empty when nothing is selected.
    /// A non-key panel cannot route Cmd+C, so the Copy button reads this.
    var selectedAnswerText: String {
        let range = answerView.selectedRange()
        guard range.length > 0, range.location != NSNotFound else { return "" }
        return (answerView.string as NSString).substring(with: range)
    }

    // ---- controls --------------------------------------------------------

    func setRunning(_ value: Bool) {
        running = value
        pauseButton.isEnabled = value
        stopButton.isEnabled = value
        pauseButton.title = "Pause"
        setAccessibilityLabel(pauseButton, "Pause observation")
    }

    func setPaused(_ paused: Bool) {
        pauseButton.title = paused ? "Resume" : "Pause"
        setAccessibilityLabel(pauseButton, paused ? "Resume observation" : "Pause observation")
    }

    func setCompact(_ value: Bool) {
        compact = value
        let oldFrame = panel.frame
        let newSize = value ? NSSize(width: 360, height: 250) : NSSize(width: 520, height: 430)
        if value {
            // Keep the compact row within 332 pt.
            stopButton.title = "Stop"
            mainButton.isHidden = true
        } else {
            stopButton.title = "Stop practice"
            mainButton.isHidden = false
        }
        // The API strip is hidden in the compact layout to save vertical space.
        apiScroll.isHidden = value
        apiExpandedHeight.isActive = !value
        apiCollapsedHeight.isActive = value
        if positioned {
            let top = oldFrame.maxY
            panel.setContentSize(newSize)
            panel.setFrameOrigin(NSPoint(x: oldFrame.minX, y: top - panel.frame.height))
        } else {
            panel.setContentSize(newSize)
        }
        compactButton.title = value ? "Expand" : "Compact"
    }

    func setStealth(_ value: Bool) {
        stealth = value
        panel.sharingType = value ? .none : .readOnly
    }

    private func setAccessibilityLabel(_ button: NSButton, _ value: String) {
        button.setAccessibilityLabel(value)
    }

    @objc private func toggleCompact() {
        setCompact(!compact)
    }

    @objc private func togglePause() {
        onPause?()
    }

    @objc private func copyAnswerAction() {
        onCopy?()
    }

    @objc private func hidePanel() {
        hide()
    }

    @objc private func showMain() {
        onShowMain?()
    }

    @objc private func stopPractice() {
        onStop?()
    }
}

// MARK: - Main application

// Native macOS shell around the Rust CLI. This window deliberately shows
// capture state and permission state; nothing is captured until the user
// explicitly starts an authorized session.
final class MainWindow: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let options: LaunchOptions
    private let panelOnly: Bool
    private var resourceRootPath: URL

    var window: NSWindow?
    var output: NSTextView?
    var provider: NSPopUpButton?
    var intervalField: NSTextField?
    var ocrCheckbox: NSButton?
    var startButton: NSButton?
    var stopButton: NSButton?
    var accessibilityLabel: NSTextField?
    var screenLabel: NSTextField?
    var answerPanel: AnswerPanel!
    var observationProcess: Process?
    var observationInput: Pipe?
    var utilityProcess: Process?
    var localAIProcess: Process?
    var globalMonitor: Any?
    var shortcutBindings: [(String, Accelerator)] = []

    let liveState = LiveState()
    private var observationBuffer = Data()
    private var pauseRequested = false
    private var quitAfterStop = false
    private var isTerminating = false

    var providerEndpoint = "https://api.openai.com/v1"
    var providerModel = "gpt-4o-mini"
    var providerApiKey = ""
    var ollamaModel = "qwen2.5-coder:1.5b"
    /// Optional Codex model name; empty lets the Codex CLI use its own default.
    var codexModel = ""
    /// Provider restored from preferences, applied once the popup is built.
    var pendingProvider: String?

    init(options: LaunchOptions) {
        self.options = options
        self.panelOnly = options.panelOnly
        self.resourceRootPath = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        super.init()
        self.resourceRootPath = Self.detectResourceRoot()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if panelOnly {
            NSApplication.shared.setActivationPolicy(.accessory)
        }
        // Restore any previously remembered provider settings before the first
        // child process is spawned.
        SavedSettings.load(into: self)
        answerPanel = AnswerPanel(stealth: options.stealth, compact: options.compact, opacity: options.opacity)
        answerPanel.onStop = { [weak self] in self?.userStoppedFromPanel() }
        answerPanel.onPause = { [weak self] in self?.togglePause() }
        answerPanel.onCopy = { [weak self] in self?.copyAnswer() }
        answerPanel.onShowMain = { [weak self] in self?.showMainWindow() }

        shortcutBindings = resolvedShortcuts()
        buildMenu()
        installGlobalMonitor()

        if panelOnly {
            answerPanel.show()
            answerPanel.setStatus("Starting · \(options.provider ?? "ollama")")
            startObservation(authorized: true)
        } else {
            buildMainWindow()
            refreshPermissions()
            if let window = window {
                window.makeKeyAndOrderFront(nil)
                NSApplication.shared.activate(ignoringOtherApps: true)
            }
        }
    }

    // ---- main window -----------------------------------------------------

    func buildMainWindow() {
        let rect = NSRect(x: 0, y: 0, width: 820, height: 640)
        let window = NSWindow(contentRect: rect,
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered, defer: false)
        window.title = "SocketStream — Practice Assistant (Stopped)"
        window.center()
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.setFrameAutosaveName("SocketStreamMainWindow")
        self.window = window

        let root = NSView(frame: rect)
        root.autoresizingMask = [.width, .height]
        window.contentView = root

        let note = NSTextField(labelWithString: "Authorized practice only. No capture is active until you explicitly start it.")
        note.frame = NSRect(x: 20, y: 610, width: 780, height: 18)
        note.font = NSFont.systemFont(ofSize: 11)
        note.textColor = .secondaryLabelColor
        root.addSubview(note)

        accessibilityLabel = NSTextField(labelWithString: "Accessibility: checking…")
        accessibilityLabel?.frame = NSRect(x: 20, y: 584, width: 430, height: 18)
        accessibilityLabel?.font = NSFont.systemFont(ofSize: 11, weight: .medium)
        root.addSubview(accessibilityLabel!)

        screenLabel = NSTextField(labelWithString: "Screen Recording: checking…")
        screenLabel?.frame = NSRect(x: 20, y: 562, width: 430, height: 18)
        screenLabel?.font = NSFont.systemFont(ofSize: 11, weight: .medium)
        root.addSubview(screenLabel!)

        let permissions = NSButton(title: "Request permissions", target: self, action: #selector(requestPermissions))
        permissions.frame = NSRect(x: 600, y: 572, width: 200, height: 26)
        permissions.bezelStyle = .rounded
        root.addSubview(permissions)

        let repair = NSButton(title: "Repair permissions", target: self, action: #selector(repairPermissions))
        repair.frame = NSRect(x: 600, y: 542, width: 200, height: 26)
        repair.bezelStyle = .rounded
        repair.toolTip = "Clear a stale permission entry left by an earlier build, then request again."
        root.addSubview(repair)

        provider = NSPopUpButton(frame: NSRect(x: 20, y: 522, width: 170, height: 30), pullsDown: false)
        provider?.addItems(withTitles: ["mock", "ollama", "codex", "openai-compatible"])
        // Explicit launch option wins; otherwise restore the remembered choice.
        let requestedProvider = options.provider ?? pendingProvider
        if let requested = requestedProvider, let popup = provider,
           let index = popup.itemTitles.firstIndex(of: requested) {
            popup.selectItem(at: index)
        }
        pendingProvider = nil
        root.addSubview(provider!)

        let intervalLabel = NSTextField(labelWithString: "Interval (s)")
        intervalLabel.frame = NSRect(x: 205, y: 530, width: 70, height: 18)
        intervalLabel.font = NSFont.systemFont(ofSize: 11)
        intervalLabel.textColor = .secondaryLabelColor
        root.addSubview(intervalLabel)

        intervalField = NSTextField(string: String(options.interval))
        intervalField?.frame = NSRect(x: 277, y: 524, width: 55, height: 26)
        intervalField?.alignment = .center
        root.addSubview(intervalField!)

        ocrCheckbox = NSButton(checkboxWithTitle: "Local OCR fallback", target: nil, action: nil)
        ocrCheckbox?.frame = NSRect(x: 345, y: 524, width: 160, height: 28)
        ocrCheckbox?.state = options.allowOCR ? .on : .off
        ocrCheckbox?.toolTip = "Capture a screenshot only when the accessibility text is insufficient."
        root.addSubview(ocrCheckbox!)

        startButton = makeButton("Start observation", #selector(startObservationPrompt), NSRect(x: 512, y: 522, width: 145, height: 30))
        startButton?.keyEquivalent = "\r"
        root.addSubview(startButton!)
        stopButton = makeButton("Stop", #selector(stopObservation), NSRect(x: 665, y: 522, width: 135, height: 30))
        stopButton?.isEnabled = false
        root.addSubview(stopButton!)

        let utility: [(String, Selector, CGFloat, CGFloat)] = [
            ("Analyze file", #selector(analyzeFile), 20, 125),
            ("Analyze screenshot", #selector(analyzeImage), 153, 165),
            ("Listen · Moonshine", #selector(listenAudio), 326, 175),
            ("Settings", #selector(showSettings), 509, 95),
            ("Show answer panel", #selector(showPanel), 612, 145),
        ]
        for (title, action, x, width) in utility {
            let button = makeButton(title, action, NSRect(x: x, y: 484, width: width, height: 28))
            root.addSubview(button)
        }

        let auxAI: [(String, Selector, CGFloat, CGFloat)] = [
            ("Start local AI", #selector(startLocalAI), 20, 120),
            ("Stop local AI", #selector(stopLocalAI), 148, 120),
            ("List models", #selector(checkProviderModels), 276, 110),
            ("Test AI response", #selector(testProvider), 394, 130),
            ("Minimize", #selector(minimizeMain), 532, 90),
        ]
        for (title, action, x, width) in auxAI {
            let button = makeButton(title, action, NSRect(x: x, y: 448, width: width, height: 28))
            root.addSubview(button)
        }

        let scroll = NSScrollView(frame: NSRect(x: 20, y: 20, width: 780, height: 416))
        scroll.autoresizingMask = [.width, .height]
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        let text = NSTextView(frame: scroll.bounds)
        text.isEditable = false
        text.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        text.string = "SocketStream ready. Grant permissions if needed, select a provider, then explicitly start an authorized screen session or analyze a file.\n"
        scroll.documentView = text
        output = text
        root.addSubview(scroll)
    }

    private func makeButton(_ title: String, _ action: Selector, _ frame: NSRect) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.frame = frame
        button.bezelStyle = .rounded
        button.controlSize = .regular
        return button
    }

    @objc func showPanel() {
        guard let panel = answerPanel else { return }
        panel.show()
        panel.setStatus(panelStatusLine())
        append("\nAnswer panel shown. It stays visible over other apps and fullscreen windows.\n")
    }

    @objc func minimizeMain() {
        // Minimizing the main window must not hide the answer panel.
        window?.miniaturize(nil)
    }

    func showMainWindow() {
        if window == nil { buildMainWindow() }
        // Closing the main window switches the normal app to accessory policy
        // so it has no Dock or app-switcher entry, while its floating panel
        // remains usable. The panel's Main button restores regular policy.
        if !panelOnly {
            NSApplication.shared.setActivationPolicy(.regular)
        }
        NSApplication.shared.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !panelOnly { showMainWindow() }
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        // Closing the main window leaves the panel and the menu-bar app alive.
        false
    }

    /// macOS routes Dock Quit and Command-Q here. For the normal app, treat
    /// that gesture as "close to background" so the live answer panel and
    /// observation session remain available. Panel-only sessions retain their
    /// explicit Stop-to-exit behavior.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !panelOnly else { return terminateNow() }
        window?.close()
        answerPanel?.show()
        NSApplication.shared.setActivationPolicy(.accessory)
        return .terminateCancel
    }

    private func terminateNow() -> NSApplication.TerminateReply {
        isTerminating = true
        SavedSettings.save(self)
        stopObservation()
        if let server = localAIProcess, server.isRunning { server.terminate() }
        if let monitor = globalMonitor {
            NSEvent.removeMonitor(monitor)
            globalMonitor = nil
        }
        return .terminateNow
    }

    /// Closing the main window must not quit: SocketStream keeps observing in the
    /// background (the answer panel and its shortcuts stay live) and the Dock
    /// icon reopens it.
    func windowWillClose(_ notification: Notification) {
        guard !isTerminating, !panelOnly else { return }
        // Accessory apps are omitted from Dock and Command-Tab while remaining
        // activatable from their own windows. The Main panel button restores
        // the standard Dock app and reopens this window.
        NSApplication.shared.setActivationPolicy(.accessory)
        answerPanel?.show()
        append("\nMain window closed. SocketStream keeps running in the background; reopen it from the Dock or the menu bar.\n")
    }

    @objc func showMainWindowAction() {
        showMainWindow()
    }

    @objc func closeMainWindowAction() {
        window?.close()
    }

    /// Dock menu: reopen or close the main window, show the answer panel, or
    /// send the regular app to the background — all without hunting menus.
    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let menu = NSMenu()
        if !panelOnly {
            let show = NSMenuItem(title: "Show main window", action: #selector(showMainWindowAction), keyEquivalent: "")
            show.target = self
            menu.addItem(show)
            let close = NSMenuItem(title: "Close main window (keeps running)", action: #selector(closeMainWindowAction), keyEquivalent: "")
            close.target = self
            menu.addItem(close)
            menu.addItem(NSMenuItem.separator())
        }
        let panel = NSMenuItem(title: "Show answer panel", action: #selector(showPanel), keyEquivalent: "")
        panel.target = self
        menu.addItem(panel)
        if observationProcess != nil {
            let stop = NSMenuItem(title: "Stop practice", action: #selector(stopObservation), keyEquivalent: "")
            stop.target = self
            menu.addItem(stop)
        }
        menu.addItem(NSMenuItem.separator())
        let quit = NSMenuItem(title: "Close to background", action: #selector(closeToBackgroundAction), keyEquivalent: "")
        quit.target = self
        menu.addItem(quit)
        return menu
    }

    @objc func closeToBackgroundAction() {
        window?.close()
        answerPanel?.show()
        NSApplication.shared.setActivationPolicy(.accessory)
    }

    // ---- permissions -----------------------------------------------------

    func refreshPermissions() {
        let ax = AXIsProcessTrusted()
        let screen = CGPreflightScreenCaptureAccess()
        accessibilityLabel?.stringValue = "Accessibility (text trees): \(ax ? "granted" : "not granted")"
        accessibilityLabel?.textColor = ax ? .systemGreen : .systemOrange
        screenLabel?.stringValue = "Screen Recording (OCR fallback): \(screen ? "granted" : "not granted")"
        screenLabel?.textColor = screen ? .systemGreen : .systemOrange
        if !ax || !screen {
            let hint = "\n\nIf SocketStream is already enabled in System Settings but this still says not granted, the stored permission no longer matches this build. macOS ties each grant to the app's signature; an ad-hoc build's signature changes on every rebuild, so the old grant silently stops matching. Use Repair permissions to clear the stale entry, then grant again. Building with a stable signing identity (scripts/create-macos-signing-cert.sh) makes the grant survive future rebuilds."
            if let output = output, !output.string.contains("stored permission no longer matches this build") {
                output.string += hint
            }
        }
    }

    @objc func requestPermissions() {
        let axOptions = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(axOptions)
        _ = CGRequestScreenCaptureAccess()
        openPrivacyPane("Privacy_Accessibility")
        append("\nPermission request sent. Grant Accessibility and Screen Recording in System Settings. If a switch is already enabled but the check still says not granted, the stored grant is stale for this build; use Repair permissions. Ad-hoc builds can require re-authorization after a rebuild.\n")
        refreshPermissions()
    }

    /// Clear this app's own stale TCC rows and re-request. This only removes the
    /// rows with the documented `tccutil`; it never grants anything and never
    /// edits the TCC database directly.
    @objc func repairPermissions() {
        let bundleID = Bundle.main.bundleIdentifier ?? "dev.socket_stream.app"
        for service in ["Accessibility", "ScreenCapture"] {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/tccutil")
            process.arguments = ["reset", service, bundleID]
            process.standardOutput = Pipe()
            process.standardError = Pipe()
            try? process.run()
            process.waitUntilExit()
        }
        append("\nStale permission entries cleared for \(bundleID). Grant both toggles again in System Settings.\n")
        requestPermissions()
    }

    func openPrivacyPane(_ anchor: String) {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(anchor)") {
            NSWorkspace.shared.open(url)
        }
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        refreshPermissions()
    }

    // ---- shortcuts -------------------------------------------------------

    /// Resolve the bindings: defaults, overridden by any valid entry in the
    /// JSON config file. Invalid entries are ignored here (the Rust CLI reports
    /// them) so the app always starts with a usable menu. `shortcuts.enabled =
    /// false` yields no bindings at all.
    func resolvedShortcuts() -> [(String, Accelerator)] {
        guard configShortcutsEnabled() else { return [] }
        var values = defaultShortcutBindings
        if let overrides = configShortcutBindings() {
            for (action, value) in overrides where values[action] != nil {
                values[action] = value
            }
        }
        return shortcutActions.compactMap { action in
            guard let raw = values[action], let accel = Accelerator.parse(raw) else { return nil }
            return (action, accel)
        }
    }

    /// Mirror `socket_stream_core::config::Config::default_path`.
    func configPath() -> URL {
        let environment = ProcessInfo.processInfo.environment
        if let override = environment["SOCKET_STREAM_CONFIG"], !override.isEmpty {
            return URL(fileURLWithPath: override)
        }
        if let xdg = environment["XDG_CONFIG_HOME"], !xdg.isEmpty {
            return URL(fileURLWithPath: xdg).appendingPathComponent("socket_stream/config.json")
        }
        if let home = environment["HOME"], !home.isEmpty {
            return URL(fileURLWithPath: home).appendingPathComponent(".config/socket_stream/config.json")
        }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".socket_stream.json")
    }

    func configShortcutBindings() -> [String: String]? {
        guard let data = try? Data(contentsOf: configPath()),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let shortcuts = root["shortcuts"] as? [String: Any],
              let bindings = shortcuts["bindings"] as? [String: String] else { return nil }
        return bindings
    }

    /// Whether the config enables shortcuts; defaults to true.
    func configShortcutsEnabled() -> Bool {
        guard let data = try? Data(contentsOf: configPath()),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let shortcuts = root["shortcuts"] as? [String: Any],
              let enabled = shortcuts["enabled"] as? Bool else { return true }
        return enabled
    }

    func shortcutTitle(_ action: String) -> String {
        switch action {
        case "start": return "Start observation"
        case "stop": return "Stop observation"
        case "pause_resume": return "Pause / resume"
        case "toggle_overlay": return "Toggle answer panel"
        case "show_overlay": return "Show answer panel"
        case "hide_overlay": return "Hide answer panel"
        case "copy_answer": return "Copy answer"
        case "compact_toggle": return "Toggle compact panel"
        case "toggle_stealth": return "Toggle stealth (capture exclusion)"
        case "open_settings": return "Open settings"
        case "refresh_question": return "Refresh question"
        case "quit": return "Quit SocketStream"
        default: return action
        }
    }

    /// Build the menu bar. Every action is clickable here, so every shortcut is
    /// reachable from the UI without a keyboard.
    func buildMenu() {
        let mainMenu = NSMenu()

        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appItem.submenu = appMenu
        appMenu.addItem(withTitle: "About SocketStream",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        if !panelOnly {
            let showMain = NSMenuItem(title: "Show main window", action: #selector(menuShowMain), keyEquivalent: "0")
            showMain.target = self
            appMenu.addItem(showMain)
        }
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Hide SocketStream", action: #selector(NSApplication.hide(_:)),
                        keyEquivalent: "h")
        appMenu.addItem(NSMenuItem.separator())
        let quitTitle = panelOnly ? "Quit SocketStream" : "Close to background"
        let quitItem = NSMenuItem(title: quitTitle,
                                  action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        quitItem.keyEquivalentModifierMask = [.command]
        appMenu.addItem(quitItem)

        let practiceItem = NSMenuItem()
        mainMenu.addItem(practiceItem)
        let practiceMenu = NSMenu(title: "Practice")
        practiceItem.submenu = practiceMenu
        for (title, action) in [
            ("Start observation", #selector(menuStart)),
            ("Stop observation", #selector(menuStop)),
            ("Pause / resume", #selector(menuPause)),
            ("Show answer panel", #selector(menuShowPanel)),
            ("Copy answer", #selector(menuCopy)),
            ("Open settings", #selector(showSettings)),
        ] {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
            item.target = self
            practiceMenu.addItem(item)
        }

        let shortcutsItem = NSMenuItem()
        mainMenu.addItem(shortcutsItem)
        let shortcutsMenu = NSMenu(title: "Shortcuts")
        shortcutsItem.submenu = shortcutsMenu
        for (index, entry) in shortcutBindings.enumerated() {
            let (action, accel) = entry
            let item = NSMenuItem(title: shortcutTitle(action),
                                  action: #selector(performShortcutMenu(_:)),
                                  keyEquivalent: accel.menuKeyEquivalent)
            item.keyEquivalentModifierMask = accel.menuModifierMask
            item.target = self
            item.tag = index
            item.toolTip = accel.display
            shortcutsMenu.addItem(item)
        }
        shortcutsMenu.addItem(NSMenuItem.separator())
        if shortcutBindings.isEmpty {
            let disabled = NSMenuItem(title: "Shortcuts are disabled in the config file",
                                      action: nil, keyEquivalent: "")
            disabled.isEnabled = false
            shortcutsMenu.addItem(disabled)
        }
        let note = NSMenuItem(
            title: "Out-of-focus keys need the desktop's own permission; the app grabs nothing else",
            action: nil, keyEquivalent: "")
        note.isEnabled = false
        shortcutsMenu.addItem(note)

        NSApplication.shared.mainMenu = mainMenu
    }

    /// Register the global key monitor once. macOS may require Accessibility
    /// permission for this; the app never bypasses that permission.
    func installGlobalMonitor() {
        guard globalMonitor == nil, !shortcutBindings.isEmpty else { return }
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            self?.handleGlobalKey(event)
        }
    }

    func handleGlobalKey(_ event: NSEvent) {
        guard let characters = event.charactersIgnoringModifiers,
              let token = Accelerator.keyToken(for: characters) else { return }
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        let accel = Accelerator(ctrl: flags.contains(.control), alt: flags.contains(.option),
                                shift: flags.contains(.shift), meta: flags.contains(.command),
                                key: token)
        guard let action = shortcutBindings.first(where: { $0.1 == accel })?.0 else { return }
        DispatchQueue.main.async { self.performAction(action) }
    }

    @objc func performShortcutMenu(_ sender: NSMenuItem) {
        guard sender.tag >= 0, sender.tag < shortcutBindings.count else { return }
        performAction(shortcutBindings[sender.tag].0)
    }

    @objc func menuStart() { startObservationPrompt() }
    @objc func menuStop() { stopObservation() }
    @objc func menuPause() { togglePause() }
    @objc func menuShowPanel() { showPanel() }
    @objc func menuCopy() { copyAnswer() }
    @objc func menuShowMain() { showMainWindow() }

    func performAction(_ action: String) {
        switch action {
        case "start":
            startObservationPrompt()
        case "stop":
            stopObservation()
        case "pause_resume":
            togglePause()
        case "toggle_overlay":
            answerPanel?.toggle()
        case "show_overlay":
            answerPanel?.show()
        case "hide_overlay":
            answerPanel?.hide()
            append("\nAnswer panel hidden (hide_overlay).\n")
        case "copy_answer":
            copyAnswer()
        case "compact_toggle":
            if let panel = answerPanel { panel.setCompact(!panel.compact) }
        case "toggle_stealth":
            if let panel = answerPanel {
                panel.setStealth(!panel.stealth)
                append("\nStealth capture exclusion: \(panel.stealth ? "on" : "off").\n")
            }
        case "open_settings":
            showSettings()
        case "refresh_question":
            append("\nRefresh requested: the observation loop picks up the next distinct question automatically.\n")
        case "quit":
            NSApplication.shared.terminate(nil)
        default:
            break
        }
    }

    // ---- paths -----------------------------------------------------------

    static func detectResourceRoot() -> URL {
        // Inside SocketStream.app/Contents/Resources.
        if let resources = Bundle.main.resourceURL, Bundle.main.bundleIdentifier != nil {
            if FileManager.default.fileExists(atPath: resources.appendingPathComponent("target/native-helpers").path) {
                return resources
            }
        }
        let executable = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
        let candidate = executable.deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        if FileManager.default.fileExists(atPath: candidate.appendingPathComponent("scripts").path) {
            return candidate
        }
        let source = URL(fileURLWithPath: #filePath)
        return source.deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
    }

    func cliPath() -> String {
        if let override = options.cli, !override.isEmpty {
            return override
        }
        if let executable = Bundle.main.executableURL {
            let sibling = executable.deletingLastPathComponent().appendingPathComponent("socket_stream")
            if FileManager.default.isExecutableFile(atPath: sibling.path) {
                return sibling.path
            }
        }
        let root = resourceRootPath
        for candidate in ["target/debug/socket_stream", "target/release/socket_stream", "bin/socket_stream"] {
            let url = root.appendingPathComponent(candidate)
            if FileManager.default.isExecutableFile(atPath: url.path) {
                return url.path
            }
        }
        return root.appendingPathComponent("target/debug/socket_stream").path
    }

    func childEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        environment["SOCKET_STREAM_BASE_URL"] = providerEndpoint
        environment["SOCKET_STREAM_MODEL"] = providerModel
        environment["SOCKET_STREAM_API_KEY"] = providerApiKey
        environment["SOCKET_STREAM_OLLAMA_MODEL"] = ollamaModel
        environment["SOCKET_STREAM_CODEX_MODEL"] = codexModel
        environment["SOCKET_STREAM_HOME"] = resourceRootPath.path
        // The AX API must run under the same signed executable that received
        // the user's Accessibility grant. A separately signed nested helper
        // gets a different TCC identity even when the app's status label says
        // permission is granted.
        if let executable = Bundle.main.executableURL?.path {
            environment["SOCKET_STREAM_MACOS_A11Y_EXECUTABLE"] = executable
        }
        return environment
    }

    func append(_ text: String) {
        DispatchQueue.main.async {
            guard let output = self.output else { return }
            output.string += text
            // Keep practice sessions bounded in memory.
            if output.string.count > 150_000 {
                output.string = String(output.string.suffix(110_000))
            }
            output.scrollToEndOfDocument(nil)
        }
    }

    // ---- observation -----------------------------------------------------

    var providerName: String {
        if let selected = provider?.titleOfSelectedItem { return selected }
        return options.provider ?? pendingProvider ?? "mock"
    }

    var observationInterval: Int {
        if let field = intervalField, let value = Int(field.stringValue) {
            return min(3600, max(2, value))
        }
        return options.interval
    }

    var allowOCR: Bool {
        ocrCheckbox?.state == .on || options.allowOCR
    }

    @objc func startObservationPrompt() {
        guard observationProcess == nil else {
            append("\nObservation is already active. Stop it first.\n")
            return
        }
        let alert = NSAlert()
        alert.messageText = "Start screen reading?"
        let ax = AXIsProcessTrusted()
        let screen = CGPreflightScreenCaptureAccess()
        var permissions: [String] = []
        if !ax { permissions.append("Accessibility is not granted, so text-tree reading will fail.") }
        if !screen && allowOCR { permissions.append("Screen Recording is not granted, so the OCR fallback will fail.") }
        alert.informativeText = "This reads the foreground window's accessible text first, then uses local on-device OCR only if you enabled the fallback. Use it only in your own practice environment or where assistance is permitted.\n\n" + permissions.joined(separator: "\n")
        alert.addButton(withTitle: "Authorize and start")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        startObservation(authorized: true)
    }

    func startObservation(authorized: Bool) {
        guard authorized else { return }
        guard observationProcess == nil else { return }
        if !FileManager.default.isExecutableFile(atPath: cliPath()) {
            append("\nsocket_stream not found at \(cliPath()). Run scripts/build-macos-helpers.sh and cargo build -p socket_stream.\n")
            answerPanel.setStatus("Error: socket_stream not found", error: true)
            answerPanel.show()
            return
        }

        var arguments = ["observe-live", "--authorized", "--interval", String(observationInterval),
                         "--provider", providerName]
        if allowOCR { arguments.append("--allow-ocr") }

        startLive(arguments)
    }

    @objc func listenAudio() {
        guard observationProcess == nil else {
            append("\nStop the active session before listening.\n")
            return
        }
        let alert = NSAlert()
        alert.messageText = "Listen with built-in Moonshine?"
        alert.informativeText = "English speech is transcribed locally and finalized phrases are sent to your selected AI provider for answers. No recording is saved. Pause/Stop end capture. Choose a source; macOS permission is required."
        alert.addButton(withTitle: "Microphone")
        alert.addButton(withTitle: "System audio")
        alert.addButton(withTitle: "Cancel")
        let choice = alert.runModal()
        guard choice == .alertFirstButtonReturn || choice == .alertSecondButtonReturn else { return }
        let source = choice == .alertFirstButtonReturn ? "microphone" : "system"
        startLive(["audio-live", "--authorized", "--stt", "moonshine", "--source", source,
                   "--provider", providerName])
    }

    func startLive(_ arguments: [String]) {
        guard observationProcess == nil else { return }

        let command = Process()
        command.executableURL = URL(fileURLWithPath: cliPath())
        command.arguments = arguments
        command.environment = childEnvironment()
        let input = Pipe()
        let stdout = Pipe()
        command.standardInput = input
        command.standardOutput = stdout
        command.standardError = stdout

        observationBuffer.removeAll()
        pauseRequested = false
        liveState.reset()
        answerPanel.setQuestion("")
        answerPanel.setAnswer("")
        answerPanel.setAPI("", response: "")
        answerPanel.setPaused(false)
        answerPanel.setRunning(true)
        answerPanel.setStatus("Starting · \(providerName)")
        answerPanel.show()

        let pendingUI = DispatchSemaphore(value: 32)
        let outputFD = stdout.fileHandleForReading.fileDescriptor
        stdout.fileHandleForReading.readabilityHandler = { [weak self, weak command] handle in
            guard pendingUI.wait(timeout: .now()) == .success else {
                handle.readabilityHandler = nil
                if command?.isRunning == true { command?.terminate() }
                return
            }
            // A readiness callback must deliver the bytes available now.
            // A read that waits to fill its buffer would delay small
            // transcript and answer events until the stream ends.
            switch readAvailablePipeBytes(outputFD) {
            case .data(let data):
                DispatchQueue.main.async {
                    defer { pendingUI.signal() }
                    self?.consumeObservationData(data)
                }
            case .wouldBlock:
                pendingUI.signal()
            case .endOfFile, .failed:
                pendingUI.signal()
                handle.readabilityHandler = nil
                if command?.isRunning == true { command?.terminate() }
            }
        }
        command.terminationHandler = { [weak self] terminated in
            stdout.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async {
                self?.observationEnded(status: terminated.terminationStatus)
            }
        }
        do {
            try command.run()
        } catch {
            answerPanel.setStatus("Error: cannot launch socket_stream", error: true)
            answerPanel.setRunning(false)
            append("\nUnable to launch CLI: \(error).\n")
            return
        }
        observationProcess = command
        observationInput = input
        window?.title = "SocketStream — Practice Assistant (ACTIVE)"
        startButton?.isEnabled = false
        stopButton?.isEnabled = true
        append("\n--- Authorized \(arguments[0]) started (\(providerName)) ---\n")
    }

    func consumeObservationData(_ data: Data) {
        observationBuffer.append(data)
        while let newline = observationBuffer.firstIndex(of: 0x0A) {
            let lineData = observationBuffer[..<newline]
            observationBuffer.removeSubrange(...newline)
            guard let line = String(data: lineData, encoding: .utf8) else { continue }
            consumeObservationLine(line)
        }
        if observationBuffer.count > 1_000_000 {
            observationBuffer.removeAll()
            append("\n[observe] dropped an oversized buffered line.\n")
        }
    }

    func consumeObservationLine(_ line: String) {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return }
        guard let data = trimmed.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            append("[observe] \(trimmed)\n")
            return
        }
        let previous = liveState.view
        guard liveState.apply(object) else { return }
        let state = liveState.view
        if !state.error.isEmpty && state.error != previous.error {
            append("[observe] error: \(state.error)\n")
        }
        if (state.status != previous.status || state.question != previous.question) && state.transcript.isEmpty {
            let shown = state.questionDisplay.isEmpty ? state.question : state.questionDisplay
            append("[observe] \(state.status)\(shown.isEmpty ? "" : " · \(shown.prefix(100))")\n")
        }
        updatePanel(state)
    }

    func updatePanel(_ state: LiveView) {
        let question = state.questionDisplay.isEmpty ? state.question : state.questionDisplay
        answerPanel.setQuestion(state.transcript.isEmpty ? question :
            "Heard: \(state.transcript.suffix(450))\nAnswering: \(question.prefix(180))")
        answerPanel.setAnswer(state.answer)
        answerPanel.setAPI(state.apiRequest, response: state.apiResponse)
        answerPanel.setStatus(panelStatusLine(), error: !state.error.isEmpty)
        answerPanel.setPaused(state.status == "paused" || pauseRequested)
        if state.status == "stopped" {
            answerPanel.setRunning(false)
        }
    }

    func panelStatusLine() -> String {
        let state = liveState.view
        if !state.error.isEmpty { return "Error: \(String(state.error.prefix(180)))" }
        var parts = [state.status.capitalized]
        if !state.source.isEmpty { parts.append(state.source) }
        if state.status != "idle" && state.status != "stopped" {
            parts.append(providerName)
        }
        return parts.joined(separator: " · ")
    }

    func togglePause() {
        guard let process = observationProcess, process.isRunning,
              let handle = observationInput?.fileHandleForWriting else { return }
        let shouldResume = pauseRequested || liveState.view.status == "paused"
        handle.write(Data((shouldResume ? "resume\n" : "pause\n").utf8))
        pauseRequested = !shouldResume
        answerPanel.setPaused(pauseRequested)
        append("\n\(shouldResume ? "Resume" : "Pause") requested.\n")
    }

    @objc func stopObservation() {
        guard let process = observationProcess else {
            append("\nNo observation process is active.\n")
            return
        }
        if let handle = observationInput?.fileHandleForWriting {
            handle.write(Data("stop\n".utf8))
        }
        append("\nStop requested.\n")
        // Give observe-live a moment to flush its final event, then terminate.
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            if process.isRunning { process.terminate() }
        }
    }

    func userStoppedFromPanel() {
        if panelOnly { quitAfterStop = true }
        stopObservation()
    }

    private func observationEnded(status: Int32) {
        observationProcess = nil
        observationInput = nil
        observationBuffer.removeAll()
        startButton?.isEnabled = true
        stopButton?.isEnabled = false
        window?.title = "SocketStream — Practice Assistant (Stopped)"
        answerPanel.setRunning(false)
        if liveState.view.status != "stopped" {
            answerPanel.setStatus("Stopped (exit \(status))")
        }
        append("\nObservation ended (exit \(status)).\n")
        if quitAfterStop && !isTerminating {
            isTerminating = true
            NSApplication.shared.terminate(nil)
        }
    }

    func copyAnswer() {
        // A non-key panel never routes Cmd+C, so copy the selection when the
        // user made one and the whole answer otherwise.
        let full = answerPanel.answerText
        let selection = answerPanel.selectedAnswerText
        let text = selection.isEmpty ? full : selection
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            answerPanel.setStatus("Nothing to copy yet")
            return
        }
        let board = NSPasteboard.general
        board.clearContents()
        board.setString(text, forType: .string)
        answerPanel.setStatus(selection.isEmpty ? "Answer copied to the clipboard"
                                                : "Selection copied to the clipboard")
        append("\nCopied to the clipboard (\(text.count) characters).\n")
    }

    // ---- utility commands ------------------------------------------------

    func runUtility(_ arguments: [String], label: String) {
        if utilityProcess != nil {
            append("\nWait for the current analysis command to finish.\n")
            return
        }
        guard FileManager.default.isExecutableFile(atPath: cliPath()) else {
            append("\nsocket_stream not found at \(cliPath()).\n")
            return
        }
        let command = Process()
        command.executableURL = URL(fileURLWithPath: cliPath())
        command.arguments = arguments
        command.environment = childEnvironment()
        let stdout = Pipe()
        command.standardOutput = stdout
        command.standardError = stdout
        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if !data.isEmpty {
                self?.append(String(decoding: data, as: UTF8.self))
            }
        }
        command.terminationHandler = { [weak self] terminated in
            stdout.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async {
                self?.utilityProcess = nil
                self?.append("\n\(label) exited: \(terminated.terminationStatus)\n")
            }
        }
        do {
            try command.run()
            utilityProcess = command
            append("\n--- \(label) ---\n")
        } catch {
            append("\nUnable to launch CLI: \(error).\n")
        }
    }

    @objc func analyzeFile() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.plainText, .sourceCode, .json]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        runUtility(["analyze-file", "--file", url.path, "--provider", providerName],
                   label: "Analyze file \(url.lastPathComponent)")
    }

    @objc func analyzeImage() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.png, .jpeg, .tiff, .heic]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        runUtility(["analyze-image", "--file", url.path, "--provider", providerName],
                   label: "OCR and analysis \(url.lastPathComponent)")
    }

    @objc func showSettings() {
        let alert = NSAlert()
        alert.messageText = "AI provider settings"
        alert.informativeText = "Credentials are passed only to the selected provider. When \"Remember on this Mac\" is on they are also stored in this app's preferences so a configured provider survives a restart; the key is never logged. Turn it off to clear anything already saved."
        alert.addButton(withTitle: "Apply")
        alert.addButton(withTitle: "Cancel")

        let form = NSView(frame: NSRect(x: 0, y: 0, width: 440, height: 253))
        let endpoint = NSTextField(string: providerEndpoint)
        let model = NSTextField(string: providerModel)
        let key = NSSecureTextField(string: providerApiKey)
        let localModel = NSTextField(string: ollamaModel)
        let codex = NSTextField(string: codexModel)
        let rows: [(String, NSTextField)] = [
            ("API base or full chat-completions URL", endpoint),
            ("Cloud model name", model),
            ("API key (optional for local endpoints)", key),
            ("Ollama model", localModel),
            ("Codex model (optional; empty uses the Codex CLI default)", codex),
        ]
        for (i, row) in rows.enumerated() {
            let y = CGFloat(213 - i * 42)
            let label = NSTextField(labelWithString: row.0)
            label.frame = NSRect(x: 0, y: y + 24, width: 420, height: 17)
            label.font = NSFont.systemFont(ofSize: 11)
            row.1.frame = NSRect(x: 0, y: y, width: 420, height: 23)
            form.addSubview(label)
            form.addSubview(row.1)
        }
        let remember = NSButton(checkboxWithTitle: "Remember on this Mac", target: nil, action: nil)
        remember.frame = NSRect(x: 0, y: 2, width: 260, height: 20)
        remember.state = SavedSettings.remember() ? .on : .off
        remember.toolTip = "Save these settings in SocketStream's own preferences so they persist across restarts."
        form.addSubview(remember)

        alert.accessoryView = form
        if alert.runModal() == .alertFirstButtonReturn {
            providerEndpoint = endpoint.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            providerModel = model.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            providerApiKey = key.stringValue
            ollamaModel = localModel.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            codexModel = codex.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            UserDefaults.standard.set(remember.state == .on, forKey: SavedSettings.rememberKey)
            SavedSettings.save(self)
            let note = remember.state == .on
                ? "Provider settings applied and remembered on this Mac (key not logged).\n"
                : "Provider settings applied for this session; saved credentials were cleared.\n"
            append("\n" + note)
        }
    }

    @objc func checkProviderModels() {
        let selected = providerName
        guard selected == "openai-compatible" || selected == "ollama" else {
            append("\nChoose OpenAI-compatible or Ollama to check the configured endpoint.\n")
            return
        }
        runUtility(["provider-models", "--provider", selected], label: "List provider models")
    }

    @objc func testProvider() {
        let alert = NSAlert()
        alert.messageText = "Test the selected AI provider?"
        alert.informativeText = "This sends a fixed test prompt and may consume provider quota. No screen or audio content is sent."
        alert.addButton(withTitle: "Send test")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        runUtility(["provider-test", "--provider", providerName, "--confirm-inference"],
                   label: "Test AI response")
    }

    @objc func audioPractice() {
        let alert = NSAlert()
        alert.messageText = "Record 15 seconds for audio practice?"
        alert.informativeText = "This records authorized audio and transcribes locally using a Whisper model. OS permission is required. No recording is retained after analysis."
        alert.addButton(withTitle: "System audio")
        alert.addButton(withTitle: "Microphone")
        alert.addButton(withTitle: "Cancel")
        let response = alert.runModal()
        guard response != .alertThirdButtonReturn else { return }
        let source = response == .alertFirstButtonReturn ? "system" : "microphone"
        var microphoneDevice = "0"
        if source == "microphone" {
            let picker = NSAlert()
            picker.messageText = "Choose microphone index"
            picker.informativeText = "Run socket_stream audio-devices in Terminal to list microphones."
            picker.addButton(withTitle: "Continue")
            picker.addButton(withTitle: "Cancel")
            let field = NSTextField(string: "0")
            field.frame = NSRect(x: 0, y: 0, width: 220, height: 26)
            picker.accessoryView = field
            guard picker.runModal() == .alertFirstButtonReturn else { return }
            microphoneDevice = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !microphoneDevice.isEmpty else { return }
        }
        let defaultModel = resourceRootPath.appendingPathComponent("models/ggml-tiny.en.bin")
        let modelURL: URL
        if FileManager.default.fileExists(atPath: defaultModel.path) {
            modelURL = defaultModel
        } else {
            let modelPanel = NSOpenPanel()
            modelPanel.message = "Select your locally installed Whisper .bin model"
            guard modelPanel.runModal() == .OK, let selectedURL = modelPanel.url else { return }
            modelURL = selectedURL
        }
        var arguments = ["audio-practice", "--authorized", "--source", source, "--duration", "15",
                         "--model", modelURL.path, "--provider", providerName]
        if source == "microphone" { arguments += ["--device", microphoneDevice] }
        runUtility(arguments, label: "Audio practice (\(source))")
    }

    @objc func startLocalAI() {
        guard localAIProcess == nil else {
            append("\nLocal Ollama launcher is already running.\n")
            return
        }
        let candidates = ["/opt/homebrew/bin/ollama", "/usr/local/bin/ollama"]
        guard let path = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
            append("\nOllama is not installed.\n")
            return
        }
        let server = Process()
        server.executableURL = URL(fileURLWithPath: path)
        server.arguments = ["serve"]
        var environment = ProcessInfo.processInfo.environment
        environment["OLLAMA_HOST"] = "127.0.0.1:11434"
        server.environment = environment
        server.standardOutput = FileHandle.nullDevice
        server.standardError = FileHandle.nullDevice
        server.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                if self?.localAIProcess === process { self?.localAIProcess = nil }
                self?.append("\nSocketStream's Ollama launcher exited.\n")
            }
        }
        do {
            try server.run()
            localAIProcess = server
            append("\nLocal AI server launch requested on localhost:11434.\n")
        } catch {
            append("\nUnable to start local AI: \(error)\n")
        }
    }

    @objc func stopLocalAI() {
        guard let owned = localAIProcess else {
            append("\nNo local AI server started by SocketStream.\n")
            return
        }
        if owned.isRunning { owned.terminate() }
        localAIProcess = nil
    }

}

// MARK: - Live output pipe

/// Result of one readiness-driven read from the CLI's output pipe.
enum PipeReadOutcome {
    case data(Data)
    case wouldBlock
    case endOfFile
    case failed
}

/// Returns the bytes available now without waiting to fill the buffer.
/// Foundation's buffered FileHandle read waits for the full count on this
/// host, which would delay small live events, so the handler uses POSIX
/// `read`.
func readAvailablePipeBytes(_ fd: Int32, capacity: Int = 16_384) -> PipeReadOutcome {
    var buffer = [UInt8](repeating: 0, count: capacity)
    var count = 0
    repeat {
        count = read(fd, &buffer, capacity)
    } while count == -1 && errno == EINTR
    if count > 0 { return .data(Data(buffer.prefix(count))) }
    if count == 0 { return .endOfFile }
    if errno == EAGAIN { return .wouldBlock } // EWOULDBLOCK == EAGAIN on Darwin
    return .failed
}

// MARK: - Self test

/// Replays the JSONL contract without AppKit so the macOS build is testable in
/// CI: `socket_stream_gui --self-test`.
enum LiveStateSelfTest {
    static func event(_ type: String, sequence: Int, session: String = "s1",
                      request: String? = nil, text: String? = nil,
                      source: String? = nil, message: String? = nil) -> [String: Any] {
        var event: [String: Any] = ["schema": 1, "type": type, "sequence": sequence, "session_id": session]
        if let request { event["request_id"] = request }
        if let text { event["text"] = text }
        if let source { event["source"] = source }
        if let message { event["message"] = message }
        return event
    }

    static func run() -> [String] {
        var failures: [String] = []
        func expect(_ condition: Bool, _ message: String) {
            if !condition { failures.append(message) }
        }

        // Class reference semantics: `let` is correct, LiveState is mutated in place.
        let state = LiveState()
        expect(state.apply(event("session_started", sequence: 1)), "session_started accepted")
        expect(state.view.status == "observing", "status observing")
        expect(state.apply(event("question_detected", sequence: 2, request: "q1",
                                 text: "Two Sum please", source: "accessibility")),
               "question_detected accepted")
        expect(state.view.status == "thinking", "status thinking")
        expect(state.view.question == "Two Sum please", "question stored")
        expect(state.view.source == "accessibility", "source stored")
        expect(!state.apply(event("answer_started", sequence: 3, request: "q1")),
               "answer_started is a no-op after question_detected")
        expect(state.apply(event("answer_delta", sequence: 4, request: "q1", text: "Use a hash map. ")),
               "first delta accepted")
        expect(state.apply(event("answer_delta", sequence: 5, request: "q1", text: "O(n).")),
               "second delta accepted")
        expect(state.view.answer == "Use a hash map. O(n).", "deltas accumulate")
        expect(state.apply(event("answer_completed", sequence: 6, request: "q1")),
               "answer_completed accepted")
        expect(state.view.status == "ready", "status ready")

        // Replays, stale sequences and foreign sessions are ignored.
        expect(!state.apply(event("answer_completed", sequence: 6, request: "q1")),
               "duplicate sequence ignored")
        expect(!state.apply(event("answer_delta", sequence: 7, request: "q1", text: "late")),
               "late delta after ready ignored")
        expect(!state.apply(event("question_detected", sequence: 8, session: "other", request: "q2",
                                  text: "Other")),
               "foreign session ignored")
        expect(state.view.answer == "Use a hash map. O(n).", "answer unchanged")

        // Pause / resume.
        expect(state.apply(event("session_paused", sequence: 9)), "pause accepted")
        expect(state.view.status == "paused", "status paused")
        expect(!state.apply(event("question_detected", sequence: 10, request: "q2", text: "Ignored")),
               "events while paused ignored")
        expect(state.apply(event("session_resumed", sequence: 11)), "resume accepted")
        expect(state.view.status == "observing", "status observing after resume")

        // A new question replaces the previous answer.
        expect(state.apply(event("question_detected", sequence: 12, request: "q3", text: "New question")),
               "second question accepted")
        expect(state.view.answer.isEmpty, "new question clears answer")

        // A role-free display rendering is preferred for the panel; without one
        // the panel falls back to the raw question text.
        expect(state.view.questionDisplay == "New question", "display falls back to question")
        var displayEvent = event("question_detected", sequence: 13, request: "q4",
                                 text: "window LeetCode\n  textarea Two Sum")
        displayEvent["display"] = "LeetCode\nTwo Sum"
        expect(state.apply(displayEvent), "question with display accepted")
        expect(state.view.question == "window LeetCode\n  textarea Two Sum", "raw question kept for the model")
        expect(state.view.questionDisplay == "LeetCode\nTwo Sum", "role-free display stored")

        // Stopped sessions accept no further work.
        expect(state.apply(event("session_stopped", sequence: 14)), "session_stopped accepted")
        expect(state.view.status == "stopped", "status stopped")
        expect(!state.apply(event("question_detected", sequence: 15, request: "q5", text: "Nope")),
               "events after stop ignored")

        // ANSI/control stripping and bounds.
        let cleaned = LiveState.clean("\u{1b}[31mred\u{1b}[0m\u{0}text", limit: 100)
        expect(cleaned == "redtext", "ANSI and control characters stripped")

        // Error events before a session are reported.
        let idle = LiveState()
        expect(idle.apply(event("error", sequence: 1, source: "provider", message: "provider offline")),
               "pre-session error accepted")
        expect(idle.view.status == "error", "error status")
        expect(idle.view.error == "provider offline", "error message")

        // Provider exchange visibility: the panel's API area.
        let api = LiveState()
        expect(api.apply(event("session_started", sequence: 1)), "api session started")
        expect(api.apply(event("question_detected", sequence: 2, request: "q1",
                               text: "Two Sum", source: "accessibility")),
               "api question accepted")
        var apiRequest = event("api_request", sequence: 3, request: "q1")
        apiRequest["url"] = "https://api.example.com/v1/chat/completions"
        apiRequest["model"] = "test-model"
        apiRequest["prompt"] = "Two Sum"
        expect(api.apply(apiRequest), "api_request accepted")
        expect(api.view.apiRequest.contains("test-model"), "api request model stored")
        expect(api.view.apiRequest.contains("chat/completions"), "api request url stored")
        var apiResponse = event("api_response", sequence: 4, request: "q1", text: "Use a hash map.")
        apiResponse["status"] = 200
        expect(api.apply(apiResponse), "api_response accepted")
        expect(api.view.apiResponse.contains("HTTP 200"), "api response status stored")
        expect(api.view.apiResponse.contains("Use a hash map."), "api response text stored")
        var staleAPI = event("api_response", sequence: 5, request: "q0", text: "nope")
        staleAPI["status"] = 500
        expect(!api.apply(staleAPI), "api_response for another request ignored")

        let speech = LiveState()
        _ = speech.apply(event("session_started", sequence: 1))
        _ = speech.apply(event("question_detected", sequence: 2, request: "spoken-1", text: "Explain search"))
        _ = speech.apply(event("answer_delta", sequence: 3, request: "spoken-1", text: "Partial"))
        expect(speech.apply(event("transcript_updated", sequence: 4, text: "\u{1b}[31mheard\u{0}")),
               "transcript without request accepted")
        expect(speech.view.transcript == "heard" && speech.view.answer == "Partial", "transcript preserves answer")
        _ = speech.apply(event("session_paused", sequence: 5))
        expect(!speech.apply(event("transcript_updated", sequence: 6, text: "paused")), "paused transcript ignored")
        _ = speech.apply(event("session_resumed", sequence: 7))
        expect(speech.apply(event("question_detected", sequence: 8, request: "spoken-2", text: "Explain search")),
               "same question with new request accepted after resume")
        expect(speech.view.answer.isEmpty, "repeated question resets partial answer")
        expect(!speech.apply(event("answer_delta", sequence: 9, request: "spoken-1", text: "stale")), "old speech answer ignored")
        expect(speech.apply(event("answer_delta", sequence: 10, request: "spoken-2", text: "Fresh")), "new speech answer accepted")
        _ = speech.apply(event("transcript_updated", sequence: 11, text: String(repeating: "x", count: 30000)))
        expect(speech.view.transcript.count == 24000, "transcript bounded")
        expect(!speech.apply(event("transcript_updated", sequence: 12, session: "other", text: "foreign")), "foreign transcript ignored")
        _ = speech.apply(event("session_stopped", sequence: 13))
        expect(!speech.apply(event("transcript_updated", sequence: 14, text: "stopped")), "stopped transcript ignored")
        _ = speech.apply(event("session_started", sequence: 15, session: "new"))
        expect(speech.view.transcript.isEmpty, "new session clears transcript")

        return failures
    }
}

/// Regression for the live-output handoff: a short event must be delivered
/// while the writer stays open, without padding the stream to a full buffer.
enum PipeReadSelfTest {
    static func run() -> [String] {
        var failures: [String] = []
        func check(_ condition: Bool, _ message: String) {
            if !condition { failures.append(message) }
        }

        // Blocking pipe, short payload, writer still open: the read must
        // return the available bytes instead of waiting for a full buffer.
        var fds: [Int32] = [0, 0]
        guard pipe(&fds) == 0 else { return ["cannot create the self-test pipe"] }
        defer { close(fds[0]) }
        let payload = Array("{\"type\":\"transcript_updated\",\"text\":\"heard\"}\n".utf8)
        let written = payload.withUnsafeBytes { buffer in
            write(fds[1], buffer.baseAddress, buffer.count)
        }
        check(written == payload.count, "self-test payload written")
        let done = DispatchSemaphore(value: 0)
        var outcome: PipeReadOutcome?
        DispatchQueue.global().async {
            outcome = readAvailablePipeBytes(fds[0])
            done.signal()
        }
        check(done.wait(timeout: .now() + 5) == .success,
              "short read returned while the writer is open")
        if case .data(let data)? = outcome {
            check(data == Data(payload), "short read returns the available bytes")
        } else {
            failures.append("short read did not return data")
        }

        // No data on a nonblocking pipe is a spurious wakeup, not an error.
        var spare: [Int32] = [0, 0]
        guard pipe(&spare) == 0 else { return failures + ["cannot create the spare pipe"] }
        defer { close(spare[0]); close(spare[1]) }
        _ = fcntl(spare[0], F_SETFL, fcntl(spare[0], F_GETFL) | O_NONBLOCK)
        if case .wouldBlock = readAvailablePipeBytes(spare[0]) {} else {
            failures.append("empty nonblocking read is reported as wouldBlock")
        }

        // Closing the writer is EOF, which ends the handler.
        close(fds[1])
        if case .endOfFile = readAvailablePipeBytes(fds[0]) {} else {
            failures.append("closed writer is reported as endOfFile")
        }

        // The production handler shape delivers a short event through a real
        // readabilityHandler while the writer stays open.
        let channel = Pipe()
        let slots = DispatchSemaphore(value: 4)
        let delivered = DispatchSemaphore(value: 0)
        var received = Data()
        let handoff = DispatchQueue(label: "socket_stream.selftest.handoff")
        channel.fileHandleForReading.readabilityHandler = { handle in
            guard slots.wait(timeout: .now()) == .success else {
                handle.readabilityHandler = nil
                return
            }
            switch readAvailablePipeBytes(handle.fileDescriptor) {
            case .data(let data):
                handoff.async {
                    defer { slots.signal() }
                    received.append(data)
                    delivered.signal()
                }
            case .wouldBlock:
                slots.signal()
            case .endOfFile, .failed:
                slots.signal()
                handle.readabilityHandler = nil
            }
        }
        channel.fileHandleForWriting.write(Data(payload))
        check(delivered.wait(timeout: .now() + 5) == .success,
              "short live event delivered while the writer stays open")
        channel.fileHandleForReading.readabilityHandler = nil
        check(received == Data(payload), "handler receives the unmodified short event")

        return failures
    }
}

// MARK: - Entry point

/// Regresses the focus contract: clicking the answer panel must never activate
/// SocketStream, because a test app watching the practice window reports any
/// activation as "out of focus". `.nonactivatingPanel` alone is not enough:
/// the selectable answer text accepts first responder, which makes the panel
/// key and activates the app. The panel type therefore refuses key status.
enum FocusSelfTest {
    static func run() -> [String] {
        var failures: [String] = []
        func expect(_ condition: Bool, _ message: String) {
            if !condition { failures.append(message) }
        }
        let panel = NonActivatingAnswerPanel(
            contentRect: NSRect(x: 0, y: 0, width: 320, height: 200),
            styleMask: [.titled, .closable, .utilityWindow, .nonactivatingPanel, .resizable],
            backing: .buffered, defer: false)
        expect(!panel.canBecomeKey, "answer panel refuses key-window status")
        expect(!panel.canBecomeMain, "answer panel refuses main-window status")
        expect(panel.styleMask.contains(.nonactivatingPanel),
               "answer panel keeps the nonactivating style mask")
        panel.close()
        return failures
    }
}

let launchOptions = LaunchOptions.parse(CommandLine.arguments)

if CommandLine.arguments.contains("--self-test") {
    let failures = LiveStateSelfTest.run() + SavedSettingsSelfTest.run() + PipeReadSelfTest.run()
        + FocusSelfTest.run()
    if failures.isEmpty {
        print("macOS panel self-test: ok")
        exit(0)
    }
    for failure in failures {
        fputs("FAIL: \(failure)\n", stderr)
    }
    exit(1)
}

#if SOCKET_STREAM_WITH_AX_READER
if let marker = CommandLine.arguments.firstIndex(of: "--socket_stream_accessibility") {
    // The child process is the same signed executable that owns the user's
    // TCC grant. It exits immediately after one explicitly requested AX read.
    let helperArguments = [CommandLine.arguments[0]]
        + CommandLine.arguments.dropFirst(marker + 1)
    exit(runMacOSAccessibility(helperArguments))
}
#endif

let app = NSApplication.shared
app.setActivationPolicy(launchOptions.panelOnly ? .accessory : .regular)
let delegate = MainWindow(options: launchOptions)
app.delegate = delegate
app.run()
