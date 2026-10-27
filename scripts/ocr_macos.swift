// OCR a scanned PDF with macOS Vision (zh-Hans + en).
// Build:  swiftc -O ocr_macos.swift -o ocr_macos -framework PDFKit -framework Vision -framework AppKit
// Usage:  ./ocr_macos input.pdf output.jsonl [firstPage] [lastPage] [dpi]
import AppKit
import Foundation
import PDFKit
import Vision

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
            if ch.value < 0x20 { out += String(format: "\\u%04x", ch.value) } else { out.unicodeScalars.append(ch) }
        }
    }
    return out
}

let args = CommandLine.arguments
guard args.count >= 3 else {
    FileHandle.standardError.write("usage: ocr_macos <in.pdf> <out.jsonl> [first] [last] [dpi]\n".data(using: .utf8)!)
    exit(2)
}
let inputURL = URL(fileURLWithPath: args[1])
let outputURL = URL(fileURLWithPath: args[2])
let first = args.count > 3 ? (Int(args[3]) ?? 1) : 1
let dpi = args.count > 5 ? (Double(args[5]) ?? 220) : 220

guard let document = PDFDocument(url: inputURL) else {
    FileHandle.standardError.write("cannot open pdf\n".data(using: .utf8)!)
    exit(1)
}
let total = document.pageCount

let last = args.count > 4 ? min(Int(args[4]) ?? total, total) : total

FileManager.default.createFile(atPath: outputURL.path, contents: nil)
let handle = try FileHandle(forWritingTo: outputURL)
defer { try? handle.close() }

let scale = dpi / 72.0
for index in (first - 1)..<last {
    guard let page = document.page(at: index) else { continue }
    let bounds = page.bounds(for: .mediaBox)
    let width = max(1, Int(bounds.width * scale))
    let height = max(1, Int(bounds.height * scale))

    guard let ctx = CGContext(
        data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
    ) else { continue }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
    ctx.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: ctx)
    guard let cgImage = ctx.makeImage() else { continue }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = true

    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do { try handler.perform([request]) } catch { continue }

    let observations = (request.results ?? []).sorted {
        if abs($0.boundingBox.midY - $1.boundingBox.midY) > 0.01 { return $0.boundingBox.midY > $1.boundingBox.midY }
        return $0.boundingBox.minX < $1.boundingBox.minX
    }
    let text = observations.compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
    let line = "{\"page\": \(index + 1), \"text\": \"\(jsonEscape(text))\"}\n"
    handle.write(line.data(using: .utf8)!)
    if (index + 1) % 25 == 0 {
        FileHandle.standardError.write("  ocr \(index + 1)/\(last)\n".data(using: .utf8)!)
    }
}
