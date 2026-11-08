import Darwin
import Foundation

func checkedTransferName(_ name: String) throws -> String {
    guard !name.isEmpty, name != ".", name != "..", !name.contains("/"), !name.utf8.contains(0), name.utf8.count <= 255 else {
        throw BoxError("Invalid file name in transfer.")
    }
    return name
}

func copyTransferBytes(_ source: Int32, _ destination: Int32, _ size: Int64) throws {
    var remaining = size
    while remaining > 0 {
        let chunk = try readExact(source, Int(min(remaining, 256 * 1024)))
        try writeAll(destination, chunk)
        remaining -= Int64(chunk.count)
    }
}

@discardableResult
func packTransfer(_ path: String, _ stream: Int32, skipSpecial: Bool = false) throws -> Int {
    var skipped = 0
    func visit(_ parent: Int32, _ name: String, _ depth: Int) throws {
        guard depth <= 256 else { throw BoxError("The transfer exceeds 256 directory levels.") }
        var info = stat()
        guard fstatat(parent, name, &info, AT_SYMLINK_NOFOLLOW) == 0 else { throw systemError("Cannot inspect transfer source") }
        let directory = info.st_mode & S_IFMT == S_IFDIR
        if !directory && info.st_mode & S_IFMT != S_IFREG {
            if skipSpecial && depth > 0 { skipped += 1; return }
            throw BoxError("Choose a regular file or directory. The transfer contains a link or special file.")
        }
        let fd = try OwnedFD(openat(parent, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC | (directory ? O_DIRECTORY : 0)))
        guard fstat(fd.value, &info) == 0, info.st_mode & S_IFMT == (directory ? S_IFDIR : S_IFREG) else {
            throw BoxError("The source changed during transfer.")
        }
        let entry = depth == 0 ? "payload" : try checkedTransferName(name)
        if directory {
            try sendPacket(stream, ["directory", entry])
            guard let listing = fdopendir(dup(fd.value)) else { throw systemError("Cannot list transfer directory") }
            defer { closedir(listing) }
            var names: [String] = []
            errno = 0
            while let child = readdir(listing) {
                let name = withUnsafePointer(to: &child.pointee.d_name) {
                    $0.withMemoryRebound(to: CChar.self, capacity: 1024) { String(validatingCString: $0) }
                }
                guard let name else { throw BoxError("A transfer file name is not valid UTF-8.") }
                if name != "." && name != ".." { names.append(name) }
                errno = 0
            }
            guard errno == 0 else { throw systemError("Cannot list transfer directory") }
            for child in names.sorted() { try visit(fd.value, child, depth + 1) }
            try sendPacket(stream, ["end"])
        } else {
            try sendPacket(stream, ["file", entry, info.st_size, info.st_mode & 0o777])
            try copyTransferBytes(fd.value, stream, info.st_size)
        }
    }
    try visit(AT_FDCWD, path, 0)
    return skipped
}

// Every directory is newly created and private. Relative openat calls and
// O_NOFOLLOW also prevent any received name from redirecting writes elsewhere.
func unpackTransfer(_ stream: Int32, _ parent: String) throws -> String {
    var stack = [try OwnedFD(open(parent, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC))]
    var first = true
    while true {
        guard let record = try receivePacket(stream, limit: 4096) as? [Any] else { throw BoxError("Invalid transfer entry.") }
        if let strings = record as? [String], strings == ["end"], stack.count > 1 {
            stack.removeLast()
        } else {
            guard [2, 4].contains(record.count), let kind = record[0] as? String, let rawName = record[1] as? String else {
                throw BoxError("Invalid transfer entry.")
            }
            let name = try checkedTransferName(rawName)
            guard !first || name == "payload" else { throw BoxError("Invalid transfer root.") }
            let directory = stack.last!.value
            if kind == "directory", record.count == 2, stack.count <= 257 {
                guard mkdirat(directory, name, 0o700) == 0 else { throw systemError("Cannot create transfer directory") }
                stack.append(try OwnedFD(openat(directory, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)))
            } else if kind == "file", record.count == 4,
                      let size = jsonInteger(record[2]), size >= 0,
                      let mode = jsonInteger(record[3]), (0...0o777).contains(mode) {
                let fd = try OwnedFD(openat(directory, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600))
                try copyTransferBytes(stream, fd.value, size)
                guard fchmod(fd.value, mode_t(mode)) == 0 else { throw systemError("Cannot set transferred file permissions") }
            } else { throw BoxError("Unexpected transfer entry or invalid file metadata.") }
        }
        first = false
        if stack.count == 1 {
            do { _ = try readExact(stream, 1); throw BoxError("Unexpected data after the transfer.") }
            catch is StreamClosed { return parent + "/payload" }
        }
    }
}

func publishTransfer(_ source: String, _ destination: String) throws {
    guard renamex_np(source, destination, UInt32(RENAME_EXCL)) == 0 else { throw systemError("Cannot publish export without replacing an existing file") }
}
