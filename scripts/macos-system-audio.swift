import Foundation
import ScreenCaptureKit
import AVFoundation
import CoreMedia

// Explicit, finite-duration system audio capture for authorized practice.
// ScreenCaptureKit enforces the operating system's permissions.
final class AudioSink: NSObject, SCStreamOutput, SCStreamDelegate {
    let writer: AVAssetWriter
    let input: AVAssetWriterInput
    private var started = false

    init(output: URL) throws {
        writer = try AVAssetWriter(outputURL: output, fileType: .m4a)
        let settings: [String: Any] = [
            AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
            AVSampleRateKey: 16000,
            AVNumberOfChannelsKey: 1,
            AVEncoderBitRateKey: 64000
        ]
        input = AVAssetWriterInput(mediaType: .audio, outputSettings: settings)
        input.expectsMediaDataInRealTime = true
        guard writer.canAdd(input) else { throw NSError(domain: "ContextLensAudio", code: 1) }
        writer.add(input)
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of outputType: SCStreamOutputType) {
        guard outputType == .audio, sampleBuffer.isValid else { return }
        if !started {
            guard writer.startWriting() else { return }
            writer.startSession(atSourceTime: sampleBuffer.presentationTimeStamp)
            started = true
        }
        if input.isReadyForMoreMediaData { _ = input.append(sampleBuffer) }
    }

    func finish() async throws {
        guard started else {
            throw NSError(domain: "ContextLensAudio", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "No audio samples received"])
        }
        input.markAsFinished()
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            writer.finishWriting { continuation.resume() }
        }
        guard writer.status == .completed else {
            throw writer.error ?? NSError(domain: "ContextLensAudio", code: 3)
        }
    }
}

@main
struct SystemAudioRecorder {
    static func main() async {
        guard CommandLine.arguments.count == 3,
              let duration = UInt64(CommandLine.arguments[1]),
              (1...900).contains(duration) else {
            fputs("Usage: context-lens-system-audio SECONDS OUTPUT.m4a (1-900)\n", stderr)
            exit(2)
        }
        let url = URL(fileURLWithPath: CommandLine.arguments[2])
        guard !FileManager.default.fileExists(atPath: url.path) else {
            fputs("Output already exists\n", stderr)
            exit(2)
        }
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false,
                                                       onScreenWindowsOnly: true)
            guard let display = content.displays.first else {
                throw NSError(domain: "ContextLensAudio", code: 4)
            }
            let filter = SCContentFilter(display: display, excludingWindows: [])
            let config = SCStreamConfiguration()
            config.capturesAudio = true
            config.excludesCurrentProcessAudio = true
            config.sampleRate = 16000
            config.channelCount = 1
            config.width = 16
            config.height = 16
            config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            let sink = try AudioSink(output: url)
            let stream = SCStream(filter: filter, configuration: config, delegate: sink)
            try stream.addStreamOutput(sink, type: .audio,
                                      sampleHandlerQueue: DispatchQueue(label: "contextlens.audio"))
            try await stream.startCapture()
            try await Task.sleep(for: .seconds(duration))
            try await stream.stopCapture()
            try await sink.finish()
            print("System audio saved to \(url.path)")
        } catch {
            fputs("System audio capture failed: \(error)\n", stderr)
            try? FileManager.default.removeItem(at: url)
            exit(3)
        }
    }
}
