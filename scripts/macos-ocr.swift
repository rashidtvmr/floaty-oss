import Foundation
import Vision
import AppKit

guard CommandLine.arguments.count == 2 else {
    fputs("Usage: macos-ocr.swift PATH_TO_SCREENSHOT\n", stderr)
    exit(2)
}
let path = CommandLine.arguments[1]
let url = URL(fileURLWithPath: path)
guard FileManager.default.fileExists(atPath: path) else {
    fputs("Image not found\n", stderr)
    exit(2)
}
let urlOptions: [VNImageOption: Any] = [:]
func recognize(_ mode: VNRequestTextRecognitionLevel) throws -> [VNRecognizedTextObservation] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = mode
    request.usesLanguageCorrection = false
    let handler = VNImageRequestHandler(url: url, options: urlOptions)
    try handler.perform([request])
    return request.results ?? []
}
do {
    let results: [VNRecognizedTextObservation]
    if ProcessInfo.processInfo.environment["SOCKET_STREAM_OCR_ACCURATE"] == "1" {
        do {
            results = try recognize(.accurate)
        } catch {
            fputs("Warning: accurate OCR failed; using fast mode.\n", stderr)
            results = try recognize(.fast)
        }
    } else {
        results = try recognize(.fast)
    }
    // Coordinate ordering: top-to-bottom; preserve separate lines.
    let observations = results.sorted {
        $0.boundingBox.midY > $1.boundingBox.midY
    }
    let recognized = observations.compactMap { observation -> (VNRecognizedTextObservation, String)? in
        guard let text = observation.topCandidates(1).first?.string, !text.isEmpty else { return nil }
        return (observation, text)
    }
    let leftEdge = recognized.map { $0.0.boundingBox.minX }.min() ?? 0.0
    let widths = recognized
        .filter { $0.1.count >= 12 }
        .map { $0.0.boundingBox.width / CGFloat($0.1.count) }
        .filter { $0 > 0.0001 }
        .sorted()
    let charWidth = widths.isEmpty ? CGFloat(0.016) : widths[widths.count / 2]
    for (observation, text) in recognized {
        // Vision trims leading whitespace; infer indentation from bounding-box X.
        let delta = max(0.0, observation.boundingBox.minX - leftEdge)
        let spaces = min(32, max(0, Int((delta / charWidth).rounded())))
        print(String(repeating: " ", count: spaces) + text)
    }
} catch {
    fputs("Native OCR failed: \(error)\n", stderr)
    exit(3)
}
