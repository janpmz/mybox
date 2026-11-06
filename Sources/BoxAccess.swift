import Darwin
import Foundation

// One connection belongs to one worker operation. It is never passed to Linux
// commands, the terminal client, or the port forwarder.
final class BoxAccessApproval {
    private var descriptor: Int32 = -1

    init(_ arguments: [String], socketPath: String = "/private/var/db/mybox-boot/access.sock",
         expectedOwner: uid_t = 0) throws {
        descriptor = socket(AF_UNIX, SOCK_STREAM, 0)
        do {
            guard descriptor >= 0, fcntl(descriptor, F_SETFD, FD_CLOEXEC) == 0 else {
                throw BoxError("Cannot connect to the MyBox approval service.")
            }
            var yes: Int32 = 1
            guard setsockopt(descriptor, SOL_SOCKET, SO_NOSIGPIPE, &yes, socklen_t(MemoryLayout.size(ofValue: yes))) == 0 else {
                throw BoxError("Cannot configure the MyBox approval connection.")
            }
            var address = sockaddr_un()
            address.sun_family = sa_family_t(AF_UNIX)
            address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
            let path = Array(socketPath.utf8) + [0]
            guard path.count <= MemoryLayout.size(ofValue: address.sun_path) else {
                throw BoxError("The MyBox approval socket path is too long.")
            }
            withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: path) }
            let connected = withUnsafePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    connect(descriptor, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
                }
            }
            var uid = uid_t.max, gid = gid_t.max
            guard connected == 0, getpeereid(descriptor, &uid, &gid) == 0, uid == expectedOwner else {
                throw BoxError("The macOS approval service is unavailable.")
            }
            try setTimeout(125)
            let challenge = (UUID().uuidString + UUID().uuidString)
                .replacingOccurrences(of: "-", with: "").lowercased()
            try sendPacket(["access-v2", challenge, arguments])
            guard try receivePacket() == ["approved", challenge] else {
                throw BoxError("Box access was not approved.")
            }
            try setTimeout(305)
        } catch {
            closeConnection()
            throw error
        }
    }

    deinit { closeConnection() }

    func firewall(_ token: String) throws {
        try sendPacket(["firewall", token])
        let reply = try receivePacket()
        if reply.count == 3, reply[0] == "firewall-error", reply[1] == token, !reply[2].isEmpty {
            throw BoxError("Firewall preparation failed: \(reply[2])")
        }
        guard reply == ["firewall", token] else {
            throw BoxError("The administrator service did not confirm this box's firewall check.")
        }
    }

    func finish() throws {
        defer { closeConnection() }
        try sendPacket(["finish"])
        guard try receivePacket() == ["finished"] else {
            throw BoxError("The approved box preparation did not finish.")
        }
    }

    private func closeConnection() {
        if descriptor >= 0 { close(descriptor); descriptor = -1 }
    }

    private func setTimeout(_ seconds: Int) throws {
        var timeout = timeval(tv_sec: seconds, tv_usec: 0)
        for option in [SO_RCVTIMEO, SO_SNDTIMEO] {
            guard setsockopt(descriptor, SOL_SOCKET, option, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout))) == 0 else {
                throw BoxError("Cannot set the MyBox approval timeout.")
            }
        }
    }

    private func sendPacket(_ value: [Any]) throws {
        let body = try JSONSerialization.data(withJSONObject: value)
        guard body.count <= 128 * 1024 else { throw BoxError("The box command is too long.") }
        var size = UInt32(body.count).bigEndian
        var data = Data(bytes: &size, count: 4)
        data.append(body)
        try data.withUnsafeBytes { bytes in
            var offset = 0
            while offset < bytes.count {
                let count = send(descriptor, bytes.baseAddress!.advanced(by: offset), bytes.count - offset, 0)
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { throw BoxError("The MyBox approval connection closed.") }
                offset += count
            }
        }
    }

    private func receivePacket() throws -> [String] {
        let header = try readBytes(4)
        let size = header.reduce(0) { ($0 << 8) | Int($1) }
        guard size > 0, size <= 128 * 1024 else { throw BoxError("Invalid MyBox approval reply.") }
        return try JSONDecoder().decode([String].self, from: readBytes(size))
    }

    private func readBytes(_ size: Int) throws -> Data {
        var data = Data(count: size)
        try data.withUnsafeMutableBytes { bytes in
            var offset = 0
            while offset < size {
                let count = recv(descriptor, bytes.baseAddress!.advanced(by: offset), size - offset, 0)
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { throw BoxError("The MyBox approval connection closed or expired.") }
                offset += count
            }
        }
        return data
    }
}

#if MYBOX_SERVICE_WORKER
var boxAccessApproval: BoxAccessApproval?
#endif

func finishBoxAccessPreparation() throws {
    #if MYBOX_SERVICE_WORKER
    try boxAccessApproval?.finish()
    #endif
}
