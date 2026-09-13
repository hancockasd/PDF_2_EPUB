import Foundation
import Vision
import AppKit

// Usage: swift ocr_vision.swift <image_path>
guard CommandLine.arguments.count >= 2 else {
    FileHandle.standardError.write("usage: ocr_vision <image>\n".data(using: .utf8)!)
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

let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do {
    try handler.perform([request])
} catch {
    FileHandle.standardError.write("VN error: \(error)\n".data(using: .utf8)!)
    exit(3)
}

guard let observations = request.results else { exit(0) }

// Sort top to bottom (Vision uses bottom-left origin, y descending)
let sorted = observations.sorted { (a, b) -> Bool in
    if abs(a.boundingBox.midY - b.boundingBox.midY) > 0.01 {
        return a.boundingBox.midY > b.boundingBox.midY
    }
    return a.boundingBox.minX < b.boundingBox.minX
}

for obs in sorted {
    if let top = obs.topCandidates(1).first {
        print(top.string)
    }
}
