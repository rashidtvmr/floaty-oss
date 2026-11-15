import Foundation
import AVFoundation
import ScreenCaptureKit
import CoreMedia
import Darwin

// Authorized, continuous 16kHz mono signed PCM to the owning CLI's pipe.
// Nonblocking writes bound queued audio and fail explicitly on overload.
final class PCMSink: NSObject, SCStreamOutput, SCStreamDelegate {
    let outputFormat = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                    sampleRate: 16000, channels: 1, interleaved: true)!
    private var converter: AVAudioConverter?
    private let lock = NSLock()

    func push(_ input: AVAudioPCMBuffer) {
        lock.lock()
        defer { lock.unlock() }
        if converter == nil || converter!.inputFormat != input.format {
            converter = AVAudioConverter(from: input.format, to: outputFormat)
        }
        guard let converter else { fail() }
        let capacity = AVAudioFrameCount(ceil(Double(input.frameLength) * 16000 / input.format.sampleRate)) + 64
        guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else { fail() }
        var supplied = false
        var error: NSError?
        let status = converter.convert(to: output, error: &error) { _, state in
            if supplied { state.pointee = .noDataNow; return nil }
            supplied = true
            state.pointee = .haveData
            return input
        }
        guard status != .error, error == nil else { fail() }
        guard output.frameLength > 0, let samples = output.int16ChannelData?[0] else { return }
        let size = Int(output.frameLength) * 2
        // No queued Dispatch blocks: if the consumer cannot keep up, terminate
        // and let the CLI report a stopped device instead of buffering forever.
        var offset = 0
        while offset < size {
            let count = Darwin.write(STDOUT_FILENO, UnsafeRawPointer(samples).advanced(by: offset), size - offset)
            if count < 0 && errno == EINTR { continue }
            guard count > 0 else { fail() }
            offset += count
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) { fail() }

    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio, sample.isValid,
              let description = sample.formatDescription else { return }
        let format = AVAudioFormat(cmAudioFormatDescription: description)
        var size = 0
        guard CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(sample,
            bufferListSizeNeededOut: &size, bufferListOut: nil, bufferListSize: 0,
            blockBufferAllocator: nil, blockBufferMemoryAllocator: nil,
            flags: 0, blockBufferOut: nil) == noErr else { fail() }
        let memory = UnsafeMutableRawPointer.allocate(byteCount: size, alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { memory.deallocate() }
        let list = memory.bindMemory(to: AudioBufferList.self, capacity: 1)
        var retained: CMBlockBuffer?
        guard CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(sample,
            bufferListSizeNeededOut: nil, bufferListOut: list, bufferListSize: size,
            blockBufferAllocator: nil, blockBufferMemoryAllocator: nil,
            flags: 0, blockBufferOut: &retained) == noErr,
            let buffer = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: list) else { fail() }
        withExtendedLifetime(retained) { push(buffer) }
    }

    private func fail() -> Never {
        fputs("Audio stream unavailable or overloaded.\n", stderr)
        exit(3)
    }
}

@main
struct AudioStream {
    static func main() async {
        let args = CommandLine.arguments
        guard args.count == 4, args[1] == "--authorized", args[2] == "--source",
              ["microphone", "system"].contains(args[3]) else {
            fputs("Usage: socket_stream_audio_stream --authorized --source microphone|system\n", stderr)
            exit(2)
        }
        signal(SIGPIPE, SIG_IGN)
        _ = fcntl(STDOUT_FILENO, F_SETFL, fcntl(STDOUT_FILENO, F_GETFL) | O_NONBLOCK)
        let parent = getppid()
        let sink = PCMSink()
        var engine: AVAudioEngine?
        var screen: SCStream?
        do {
            if args[3] == "microphone" {
                guard await AVCaptureDevice.requestAccess(for: .audio) else { throw CaptureError.denied }
                let audio = AVAudioEngine()
                let node = audio.inputNode
                let format = node.outputFormat(forBus: 0)
                guard format.sampleRate > 0, format.channelCount > 0 else { throw CaptureError.denied }
                node.installTap(onBus: 0, bufferSize: 1600, format: format) { buffer, _ in sink.push(buffer) }
                try audio.start()
                engine = audio
            } else {
                let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
                guard let display = content.displays.first else { throw CaptureError.denied }
                let config = SCStreamConfiguration()
                config.capturesAudio = true
                config.excludesCurrentProcessAudio = true
                config.sampleRate = 16000
                config.channelCount = 1
                config.width = 16
                config.height = 16
                config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
                let stream = SCStream(filter: SCContentFilter(display: display, excludingWindows: []),
                                      configuration: config, delegate: sink)
                try stream.addStreamOutput(sink, type: .audio,
                                           sampleHandlerQueue: DispatchQueue(label: "socket_stream.audio.pcm"))
                try await stream.startCapture()
                screen = stream
            }
            // The CLI also owns/terminates this process. Handle abrupt parent
            // death so a force-quit cannot leave an orphaned capture helper.
            while getppid() == parent && parent > 1 {
                try await Task.sleep(for: .milliseconds(250))
            }
            engine?.stop()
            if let screen { try await screen.stopCapture() }
        } catch {
            fputs("Audio capture failed; check OS permission and input device.\n", stderr)
            exit(3)
        }
    }
    enum CaptureError: Error { case denied }
}
