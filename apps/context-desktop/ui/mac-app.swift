import AppKit
import Foundation
import UniformTypeIdentifiers

// Native macOS shell around the Rust CLI. This window deliberately shows capture state.
final class MainWindow: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var output: NSTextView!
    var provider: NSPopUpButton!
    var process: Process?
    var localAIProcess: Process?
    var authorized = false
    var providerEndpoint = "https://api.openai.com/v1"
    var providerModel = "gpt-4o-mini"
    var providerApiKey = ""
    var ollamaModel = "qwen2.5-coder:1.5b"

    func applicationDidFinishLaunching(_ notification: Notification) {
        let rect = NSRect(x: 0, y: 0, width: 760, height: 550)
        window = NSWindow(contentRect: rect, styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "ContextLens — Screen/text practice assistant (Stopped)"
        window.center()
        window.isReleasedWhenClosed = false

        let root = NSView(frame: rect)
        root.autoresizingMask = [.width, .height]
        window.contentView = root

        let label = NSTextField(labelWithString: "Authorized practice only. No capture is active until you explicitly start it.")
        label.frame = NSRect(x: 20, y: 504, width: 710, height: 22)
        label.font = NSFont.systemFont(ofSize: 12)
        label.textColor = .secondaryLabelColor
        root.addSubview(label)

        provider = NSPopUpButton(frame: NSRect(x: 20, y: 462, width: 160, height: 31), pullsDown: false)
        provider.addItems(withTitles: ["mock", "ollama", "codex", "openai-compatible"])
        root.addSubview(provider)

        let buttons: [(String, Selector, CGFloat, CGFloat)] = [
            ("Start observation", #selector(startObservation), 188, 175),
            ("Stop", #selector(stopObservation), 370, 66),
            ("Analyze file", #selector(analyzeFile), 443, 115),
            ("Settings", #selector(showSettings), 565, 125)
        ]
        for (name, action, x, width) in buttons {
            let button = NSButton(title: name, target: self, action: action)
            button.frame = NSRect(x: x, y: 462, width: width, height: 30)
            button.bezelStyle = .rounded
            root.addSubview(button)
        }

        let imageButton = NSButton(title: "Analyze screenshot", target: self, action: #selector(analyzeImage))
        imageButton.frame = NSRect(x: 20, y: 425, width: 160, height: 28)
        root.addSubview(imageButton)
        let audioButton = NSButton(title: "System audio practice", target: self, action: #selector(audioPractice))
        audioButton.frame = NSRect(x: 188, y: 425, width: 180, height: 28)
        root.addSubview(audioButton)
        let startAI = NSButton(title: "Start local AI", target: self, action: #selector(startLocalAI))
        startAI.frame = NSRect(x: 378, y: 425, width: 148, height: 28)
        root.addSubview(startAI)
        let stopAI = NSButton(title: "Stop local AI", target: self, action: #selector(stopLocalAI))
        stopAI.frame = NSRect(x: 533, y: 425, width: 150, height: 28)
        root.addSubview(stopAI)

        let modelCheck = NSButton(title: "List provider models", target: self, action: #selector(checkProviderModels))
        modelCheck.frame = NSRect(x: 20, y: 388, width: 190, height: 28)
        root.addSubview(modelCheck)
        let inferenceCheck = NSButton(title: "Test AI response", target: self, action: #selector(testProvider))
        inferenceCheck.frame = NSRect(x: 220, y: 388, width: 170, height: 28)
        root.addSubview(inferenceCheck)

        let scroll = NSScrollView(frame: NSRect(x: 20, y: 20, width: 720, height: 355))
        scroll.autoresizingMask = [.width, .height]
        scroll.hasVerticalScroller = true
        output = NSTextView(frame: scroll.bounds)
        output.isEditable = false
        output.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        output.string = "ContextLens ready. Select a provider and use Analyze file, or explicitly start an authorized screen session.\n"
        scroll.documentView = output
        root.addSubview(scroll)
        window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    func cliPath() -> String {
        // Prefer runtime path: compiled executable lives in target/native-helpers.
        let executable = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
        let project = executable.deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent()
        let compiledCLI = project.appendingPathComponent("target/debug/context-cli")
        if FileManager.default.fileExists(atPath: compiledCLI.path) {
            return compiledCLI.path
        }
        // Development fallback for `swift source.swift`.
        let source = URL(fileURLWithPath: #filePath)
        return source.deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("target/debug/context-cli").path
    }

    func append(_ text: String) {
        DispatchQueue.main.async {
            self.output.string += text
            // Keep practice sessions bounded in memory.
            if self.output.string.count > 150_000 {
                self.output.string = String(self.output.string.suffix(110_000))
            }
            self.output.scrollToEndOfDocument(nil)
        }
    }

    func execute(_ arguments: [String], observation: Bool) {
        if process != nil {
            append("\nStop the active command before starting another.\n")
            return
        }
        let command = Process()
        command.executableURL = URL(fileURLWithPath: cliPath())
        command.arguments = arguments
        var environment = ProcessInfo.processInfo.environment
        environment["CONTEXT_LENS_BASE_URL"] = providerEndpoint
        environment["CONTEXT_LENS_MODEL"] = providerModel
        environment["CONTEXT_LENS_API_KEY"] = providerApiKey
        environment["CONTEXT_LENS_OLLAMA_MODEL"] = ollamaModel
        command.environment = environment
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
                self?.process = nil
                self?.window.title = "ContextLens — Screen/text practice assistant (Stopped)"
                self?.authorized = false
                self?.append("\nProcess exited: \(terminated.terminationStatus)\n")
            }
        }
        do {
            try command.run()
            process = command
            if observation {
                window.title = "ContextLens — Authorized session ACTIVE"
            }
        } catch {
            append("\nUnable to launch CLI: \(error). Build with cargo build -p context-cli.\n")
        }
    }

    @objc func startObservation() {
        let alert = NSAlert()
        alert.messageText = "Start screen reading?"
        alert.informativeText = "This reads foreground screen text through Accessibility, using OCR as needed. Use it only in your own practice environment or where assistance is permitted. Your OS may request Screen Recording and Accessibility permissions."
        alert.addButton(withTitle: "Authorize and start")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        authorized = true
        append("\n--- Explicitly authorized screen observation ---\n")
        execute(["observe", "--authorized", "--interval", "5", "--max-captures", "1000", "--provider", provider.titleOfSelectedItem ?? "mock"], observation: true)
    }

    @objc func stopObservation() {
        if let process = process {
            process.terminate()
            append("\nStop requested.\n")
        }
        authorized = false
    }

    @objc func analyzeFile() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.plainText, .sourceCode, .json]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        append("\n--- Analyze user-selected file \(url.lastPathComponent) ---\n")
        execute(["analyze-file", "--file", url.path, "--provider", provider.titleOfSelectedItem ?? "mock"], observation: false)
    }

    @objc func analyzeImage() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.png, .jpeg, .tiff, .heic]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        append("\n--- OCR and analysis: \(url.lastPathComponent) ---\n")
        execute(["analyze-image", "--file", url.path, "--provider", provider.titleOfSelectedItem ?? "mock"], observation: false)
    }

    @objc func showSettings() {
        let alert = NSAlert()
        alert.messageText = "AI provider settings"
        alert.informativeText = "Credentials are held in this app's memory and passed only to the selected provider. They are not saved to disk."
        alert.addButton(withTitle: "Apply")
        alert.addButton(withTitle: "Cancel")

        let form = NSView(frame: NSRect(x: 0, y: 0, width: 440, height: 205))
        let endpoint = NSTextField(string: providerEndpoint)
        let model = NSTextField(string: providerModel)
        let key = NSSecureTextField(string: providerApiKey)
        let localModel = NSTextField(string: ollamaModel)
        let rows: [(String, NSTextField)] = [
            ("API base or full chat-completions URL", endpoint),
            ("Cloud model name", model),
            ("API key (optional for local endpoints)", key),
            ("Ollama model", localModel)
        ]
        for (i, row) in rows.enumerated() {
            let y = CGFloat(165 - i * 48)
            let label = NSTextField(labelWithString: row.0)
            label.frame = NSRect(x: 0, y: y + 26, width: 420, height: 19)
            row.1.frame = NSRect(x: 0, y: y, width: 420, height: 25)
            form.addSubview(label)
            form.addSubview(row.1)
        }
        alert.accessoryView = form
        if alert.runModal() == .alertFirstButtonReturn {
            providerEndpoint = endpoint.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            providerModel = model.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            providerApiKey = key.stringValue
            ollamaModel = localModel.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            append("\nProvider settings updated for this session (key not logged).\n")
        }
    }

    @objc func checkProviderModels() {
        let selected = provider.titleOfSelectedItem ?? "mock"
        guard selected == "openai-compatible" || selected == "ollama" else {
            append("\nChoose OpenAI-compatible or Ollama to check the configured endpoint.\n")
            return
        }
        execute(["provider-models", "--provider", selected], observation: false)
    }

    @objc func testProvider() {
        let alert = NSAlert()
        alert.messageText = "Test the selected AI provider?"
        alert.informativeText = "This sends a fixed test prompt and may consume provider quota. No screen or audio content is sent."
        alert.addButton(withTitle: "Send test")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        execute(["provider-test", "--provider", provider.titleOfSelectedItem ?? "mock", "--confirm-inference"], observation: false)
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
            picker.informativeText = "Run context-cli audio-devices in Terminal to list microphones."
            picker.addButton(withTitle: "Continue")
            picker.addButton(withTitle: "Cancel")
            let field = NSTextField(string: "0")
            field.frame = NSRect(x: 0, y: 0, width: 220, height: 26)
            picker.accessoryView = field
            guard picker.runModal() == .alertFirstButtonReturn else { return }
            microphoneDevice = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !microphoneDevice.isEmpty else { return }
        }
        let defaultModel = URL(fileURLWithPath: cliPath()).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("models/ggml-tiny.en.bin")
        let modelURL: URL
        if FileManager.default.fileExists(atPath: defaultModel.path) {
            modelURL = defaultModel
        } else {
            let modelPanel = NSOpenPanel()
            modelPanel.message = "Select your locally installed Whisper .bin model"
            guard modelPanel.runModal() == .OK, let selectedURL = modelPanel.url else { return }
            modelURL = selectedURL
        }
        var arguments = ["audio-practice", "--authorized", "--source", source, "--duration", "15", "--model", modelURL.path,
                         "--provider", provider.titleOfSelectedItem ?? "mock"]
        if source == "microphone" { arguments += ["--device", microphoneDevice] }
        execute(arguments, observation: false)
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
                self?.append("\nContextLens's Ollama launcher exited.\n")
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
            append("\nNo local AI server started by ContextLens.\n")
            return
        }
        if owned.isRunning { owned.terminate() }
        localAIProcess = nil
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        stopObservation()
        if let server = localAIProcess, server.isRunning { server.terminate() }
        return .terminateNow
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = MainWindow()
app.delegate = delegate
app.run()
