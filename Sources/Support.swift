import Foundation
import Darwin

struct BoxError: Error, CustomStringConvertible {
    let description: String
    init(_ message: String) { description = message }
}

struct CommandFailed: Error {
    let status: Int32
}

let files = FileManager.default

func note(_ message: String) {
    FileHandle.standardError.write(Data((message + "\n").utf8))
}

func matches(_ value: String, _ pattern: String) -> Bool {
    guard let range = value.range(of: pattern, options: .regularExpression) else { return false }
    return range == value.startIndex..<value.endIndex
}
