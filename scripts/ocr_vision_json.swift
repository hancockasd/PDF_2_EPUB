import Foundation
import Vision
import AppKit

// Usage: ocr_vision_json <image_path>
// Emits one JSON object per recognized line:
// {"text":..., "x":..., "y":..., "w":..., "h":..., "conf":...}
// Coordinates are normalized 0..1 with origin at TOP-LEFT.
guard CommandLine.arguments.count >= 2 else {
    FileHandle.standardError.write("usage: ocr_vision_json <image>\n".data(using: .utf8)!)
    exit(1)
}

let path = CommandLine.arguments[1]
guard let img = NSImage(contentsOfFile: path),
      let tiff = img.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiff),
      let cg = bitmap.cgImage else {
    FileHandle.standardError.write("failed to load image: \(path)\n".data(using: .utf8)!)
    exit(2)
}

let request = VNRecognizeTextRequest()
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.minimumTextHeight = 0.004

let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do {
    try handler.perform([request])
} catch {
    FileHandle.standardError.write("VN error: \(error)\n".data(using: .utf8)!)
    exit(3)
}

guard let observations = request.results else { exit(0) }

func jsonEscape(_ s: String) -> String {
    var out = ""
    for ch in s.unicodeScalars {
        switch ch {
        case "\"": out += "\\\""
        case "\\": out += "\\\\"
        case "\n": out += "\\n"
        case "\r": out += "\\r"
        case "\t": out += "\\t"
        default:
            if ch.value < 0x20 {
                out += String(format: "\\u%04x", ch.value)
            } else {
                out.unicodeScalars.append(ch)
            }
        }
    }
    return out
}

for obs in observations {
    guard let top = obs.topCandidates(1).first else { continue }
    let bb = obs.boundingBox
    let y = 1.0 - Double(bb.origin.y) - Double(bb.size.height)
    let line = String(format:
        "{\"text\":\"%@\",\"x\":%.5f,\"y\":%.5f,\"w\":%.5f,\"h\":%.5f,\"conf\":%.3f}",
        jsonEscape(top.string), Double(bb.origin.x), y,
        Double(bb.size.width), Double(bb.size.height), Double(top.confidence))
    print(line)
}
