import Darwin
import Foundation

@main struct BrokerMain {
    static func main() {
        do {
            let args = Array(CommandLine.arguments.dropFirst())
            switch args.first {
            case "client": exit(try brokerClient(Array(args.dropFirst())))
            case "serve" where args.count == 1: try serveBroker()
            case "session" where args.count == 4:
                guard let fd = Int32(args[1]), let owner = UInt32(args[2]) else { throw BoxError("Invalid session arguments.") }
                let connection = try OwnedFD(fd)
                // A client may reject the service or cancel before this process
                // starts. Darwin can then reject socket options with EINVAL.
                do { try configureSocket(connection.value) } catch { return }
                do { try serveSession(connection.value, owner: owner, worker: args[3]) }
                catch { try? sendPacket(connection.value, ["error", String(describing: error)]) }
            default: throw BoxError("Unknown service operation.")
            }
        } catch { note("MyBox: \(error)"); exit(1) }
    }
}
