// Draws a picture for every word in tools/images/.cache/words.tsv with Apple's Image Playground
// on this Mac (Apple Intelligence must be on), into tools/images/.cache/<style>/<name>.png,
// and writes tools/images/.cache/<style>/index.html to look them over.
//
//     swiftc -parse-as-library -O tools/images/playground.swift -o tools/images/.cache/playground
//     tools/images/.cache/playground [animation|illustration|sketch]
import AppKit
import Foundation
import ImagePlayground

@main
struct Playground {
    static func main() async throws {
        let cache = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appending(path: ".cache")
        let styleName = CommandLine.arguments.dropFirst().first ?? "illustration"
        let creator = try await ImageCreator()
        guard let style = creator.availableStyles.first(where: { "\($0)".lowercased().contains(styleName) }) else {
            print("No style \(styleName); available: \(creator.availableStyles)")
            exit(1)
        }
        let out = cache.appending(path: styleName)
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

        let rows = try String(contentsOf: cache.appending(path: "words.tsv"), encoding: .utf8)
            .split(separator: "\n").map { $0.split(separator: "\t", omittingEmptySubsequences: false).map(String.init) }
        var cells: [String] = []
        var failed = 0
        let started = Date()
        for row in rows where row.count >= 4 {
            let (name, english, pos, russian) = (row[0], row[1], row[2], row[3])
            let file = out.appending(path: "\(name).png")
            var note = ""
            if !FileManager.default.fileExists(atPath: file.path) {
                do {
                    for try await image in creator.images(for: [.text(prompt(english, pos))], style: style, limit: 1) {
                        let rep = NSBitmapImageRep(cgImage: image.cgImage)
                        try rep.representation(using: .png, properties: [:])!.write(to: file)
                    }
                    print("ok   \(english)")
                } catch {
                    failed += 1
                    note = "\(error)"
                    print("fail \(english): \(error)")
                }
            }
            let picture = FileManager.default.fileExists(atPath: file.path)
                ? "<img src=\"\(name).png\">" : "<div class=none>\(note.isEmpty ? "—" : note)</div>"
            cells.append("<figure>\(picture)<figcaption><b>\(english)</b> \(pos)<br>\(russian)</figcaption></figure>")
        }
        let html = """
        <!doctype html><meta charset=utf-8><title>\(styleName)</title>
        <style>body{font:14px -apple-system;display:grid;grid-template-columns:repeat(auto-fill,180px);gap:12px;padding:16px}
        figure{margin:0}img,.none{width:180px;height:180px;object-fit:contain;background:#f4f4f4;border-radius:8px}
        .none{display:flex;align-items:center;justify-content:center;font-size:11px;color:#a00;text-align:center}</style>
        \(cells.joined(separator: "\n"))
        """
        try html.write(to: out.appending(path: "index.html"), atomically: true, encoding: .utf8)
        print("\(rows.count) words, \(failed) failed, \(Int(Date().timeIntervalSince(started))) s → \(out.path)/index.html")
    }

    /// One object, centred, plain background: a picture that reads at the size of a card.
    static func prompt(_ english: String, _ pos: String) -> String {
        switch pos {
        case "adjective": "a person or thing that is \(english), simple, centred, plain white background"
        case "verb": "someone who is about to \(english), simple, centred, plain white background"
        default: "\(english), a single object, simple, centred, plain white background"
        }
    }
}
