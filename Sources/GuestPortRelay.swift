import Foundation
import Darwin

// The guest receives framed input. A TCP half-close is an F frame, while actual
// stdin EOF means cancellation. This also ends the guest after the Mac helper exits.
// Each direction uses bounded 64 KiB payload buffers and small frame headers.
let guestPortRelayProgram = #"""
use strict; use warnings;
use IO::Socket::INET; use IO::Select;
use Fcntl qw(F_GETFL F_SETFL O_NONBLOCK);
use Errno qw(EAGAIN EWOULDBLOCK EINTR);
$SIG{PIPE} = 'IGNORE';
my $port = shift;
die "Invalid inside port\n" unless defined($port) && $port =~ /\A[0-9]+\z/ && $port > 0 && $port < 65536;
my $peer = IO::Socket::INET->new(PeerAddr=>'127.0.0.1', PeerPort=>$port, Proto=>'tcp', Timeout=>3)
    or die "No server on the box's 127.0.0.1:$port: $!\n";
binmode STDIN; binmode STDOUT;
for my $handle (\*STDIN, \*STDOUT, $peer) {
    my $flags = fcntl($handle, F_GETFL, 0);
    defined($flags) && fcntl($handle, F_SETFL, $flags | O_NONBLOCK) or die "Cannot configure relay I/O: $!\n";
}
my ($input, $to_peer, $output) = ('', '', pack('aN', 'R', 0));
my ($input_end, $peer_end, $write_end, $output_end) = (0, 0, 0, 0);
while (1) {
    if (length($input) >= 5 && length($to_peer) == 0) {
        my ($kind, $size) = unpack('aN', substr($input, 0, 5));
        die "Invalid relay frame\n" if $size > 65536 || $input_end ||
            !(($kind eq 'D' && $size > 0) || ($kind eq 'F' && $size == 0));
        if (length($input) >= 5 + $size) {
            substr($input, 0, 5, '');
            if ($kind eq 'D') { $to_peer = substr($input, 0, $size, ''); }
            else { $input_end = 1; }
        }
    }
    if ($input_end && !$write_end && length($to_peer) == 0) {
        shutdown($peer, 1); $write_end = 1;
    }
    if ($peer_end && !$output_end && length($output) == 0) {
        $output = pack('aN', 'F', 0); $output_end = 1;
    }
    last if $write_end && $output_end && length($output) == 0;
    my $reads = IO::Select->new();
    my $writes = IO::Select->new();
    # Keep observing stdin after F so loss of the Mac connection cancels the relay.
    $reads->add(\*STDIN) if length($input) < 65541 && length($to_peer) == 0;
    $reads->add($peer) if !$peer_end && length($output) == 0;
    $writes->add($peer) if length($to_peer);
    $writes->add(\*STDOUT) if length($output);
    my ($ready_read, $ready_write) = IO::Select->select($reads, $writes, undef, 30);
    next unless defined($ready_read);
    for my $handle (@$ready_read) {
        my $stdin = fileno($handle) == fileno(STDIN);
        my $count = sysread($handle, my $data, $stdin ? 65541 - length($input) : 65536);
        if (!defined($count)) { next if $! == EAGAIN || $! == EWOULDBLOCK || $! == EINTR; die "Relay read failed: $!\n"; }
        if ($stdin) { exit 0 if $count == 0; $input .= $data; }
        elsif ($count == 0) { $peer_end = 1; }
        else { $output = pack('aN', 'D', $count) . $data; }
    }
    for my $handle (@$ready_write) {
        my $buffer = fileno($handle) == fileno(STDOUT) ? \$output : \$to_peer;
        my $count = syswrite($handle, $$buffer);
        if (!defined($count)) { next if $! == EAGAIN || $! == EWOULDBLOCK || $! == EINTR; die "Relay write failed: $!\n"; }
        die "Relay write stopped\n" if $count == 0;
        substr($$buffer, 0, $count, '');
    }
}
"""#

// All state and file descriptor operations use the relay's serial queue.
// Nonblocking pipes keep a slow guest from blocking other mappings or monitoring.
final class GuestPortConnection {
    typealias Receive = (Data?, Bool, Error?) -> Void
    let process = Process()
    let input = Pipe()
    let output = Pipe()
    let queue: DispatchQueue
    var reader: DispatchSourceRead?
    var writer: DispatchSourceWrite?
    var readSuspended = true
    var writeSuspended = true
    var buffer = Data()
    var pendingRead: ((UInt8, Data) -> Void)?
    var readFailure: ((Error) -> Void)?
    var pendingWrite: Data?
    var written = 0
    var writeCompletion: ((Error?) -> Void)?
    var ready = false
    var receivedEnd = false
    var cancelled = false

    init(backend: String, id: String, port: Int, queue: DispatchQueue) throws {
        self.queue = queue
        process.executableURL = URL(fileURLWithPath: backend)
        process.arguments = ["exec", "--interactive", "--user", "0", id, "/usr/bin/perl", "-e", guestPortRelayProgram, String(port)]
        process.standardInput = input.fileHandleForReading
        process.standardOutput = output.fileHandleForWriting
        process.standardError = FileHandle.standardError
        try process.run()
        input.fileHandleForReading.closeFile()
        output.fileHandleForWriting.closeFile()
        let readFD = output.fileHandleForReading.fileDescriptor
        let writeFD = input.fileHandleForWriting.fileDescriptor
        guard fcntl(readFD, F_SETFL, O_NONBLOCK) == 0, fcntl(writeFD, F_SETFL, O_NONBLOCK) == 0 else {
            input.fileHandleForWriting.closeFile()
            output.fileHandleForReading.closeFile()
            process.terminate()
            throw BoxError("Cannot configure the box loopback connection.")
        }
        let read = DispatchSource.makeReadSource(fileDescriptor: readFD, queue: queue)
        read.setEventHandler { [weak self] in self?.readAvailable() }
        let readHandle = output.fileHandleForReading
        read.setCancelHandler { readHandle.closeFile() }
        reader = read
        let write = DispatchSource.makeWriteSource(fileDescriptor: writeFD, queue: queue)
        write.setEventHandler { [weak self] in self?.writeAvailable() }
        let writeHandle = input.fileHandleForWriting
        write.setCancelHandler { writeHandle.closeFile() }
        writer = write
    }

    func start(_ completion: @escaping (Error?) -> Void) {
        readFrame({ [weak self] kind, payload in
            guard let self else { return }
            guard kind == 82 && payload.isEmpty else { completion(BoxError("Invalid box relay response.")); return }
            self.ready = true
            completion(nil)
        }, failure: completion)
        queue.asyncAfter(deadline: .now() + 5) { [weak self] in
            guard let self, !self.cancelled, !self.ready else { return }
            self.failRead(BoxError("The box loopback connection did not become ready."))
        }
    }

    func receive(_ completion: @escaping Receive) {
        if receivedEnd { completion(nil, true, nil); return }
        readFrame({ [weak self] kind, payload in
            if kind == 68 && !payload.isEmpty { completion(payload, false, nil) }
            else if kind == 70 && payload.isEmpty { self?.receivedEnd = true; completion(nil, true, nil) }
            else { completion(nil, false, BoxError("Invalid box relay data.")) }
        }, failure: { completion(nil, false, $0) })
    }

    func readFrame(_ completion: @escaping (UInt8, Data) -> Void, failure: @escaping (Error) -> Void) {
        guard !cancelled else { failure(BoxError("The box connection closed.")); return }
        precondition(pendingRead == nil)
        pendingRead = completion
        readFailure = failure
        deliverFrame()
        if pendingRead != nil && readSuspended { readSuspended = false; reader?.resume() }
    }

    func deliverFrame() {
        guard pendingRead != nil, buffer.count >= 5 else { return }
        let bytes = [UInt8](buffer.prefix(5))
        let size = bytes[1...4].reduce(0) { ($0 << 8) | Int($1) }
        guard size <= 65536 else { failRead(BoxError("The box relay frame is too large.")); return }
        guard buffer.count >= 5 + size else { return }
        let payload = Data(buffer.dropFirst(5).prefix(size))
        buffer = Data(buffer.dropFirst(5 + size))
        let completion = pendingRead
        pendingRead = nil
        readFailure = nil
        if !readSuspended { readSuspended = true; reader?.suspend() }
        completion?(bytes[0], payload)
    }

    func readAvailable() {
        guard !cancelled, pendingRead != nil else { return }
        let capacity = 65541 - buffer.count
        guard capacity > 0 else { failRead(BoxError("The box relay buffer is full.")); return }
        var bytes = [UInt8](repeating: 0, count: min(65536, capacity))
        let count = Darwin.read(output.fileHandleForReading.fileDescriptor, &bytes, bytes.count)
        if count > 0 { buffer.append(contentsOf: bytes.prefix(count)); deliverFrame() }
        else if count == 0 { failRead(BoxError("The box loopback connection closed.")) }
        else if errno != EAGAIN && errno != EINTR { failRead(BoxError("Cannot read the box loopback connection.")) }
    }

    func failRead(_ error: Error) {
        let failure = readFailure
        pendingRead = nil
        readFailure = nil
        if !readSuspended { readSuspended = true; reader?.suspend() }
        failure?(error)
    }

    func send(_ data: Data?, final: Bool, completion: @escaping (Error?) -> Void) {
        guard !cancelled else { completion(BoxError("The box connection closed.")); return }
        precondition(pendingWrite == nil)
        var frame = Data()
        if let data, !data.isEmpty {
            let count = UInt32(data.count)
            frame.append(contentsOf: [68, UInt8((count >> 24) & 255), UInt8((count >> 16) & 255), UInt8((count >> 8) & 255), UInt8(count & 255)])
            frame.append(data)
        }
        if final { frame.append(contentsOf: [70, 0, 0, 0, 0]) }
        if frame.isEmpty { completion(nil); return }
        pendingWrite = frame
        written = 0
        writeCompletion = completion
        writeAvailable()
    }

    func writeAvailable() {
        guard !cancelled, let data = pendingWrite else { return }
        let count = data.withUnsafeBytes { bytes in
            Darwin.write(input.fileHandleForWriting.fileDescriptor, bytes.baseAddress!.advanced(by: written), data.count - written)
        }
        if count > 0 { written += count }
        if count < 0 && errno != EAGAIN && errno != EINTR {
            finishWrite(BoxError("Cannot write to the box loopback connection."))
        } else if written == data.count { finishWrite(nil) }
        else if writeSuspended { writeSuspended = false; writer?.resume() }
    }

    func finishWrite(_ error: Error?) {
        pendingWrite = nil
        let completion = writeCompletion
        writeCompletion = nil
        if !writeSuspended { writeSuspended = true; writer?.suspend() }
        completion?(error)
    }

    func cancel() {
        guard !cancelled else { return }
        cancelled = true
        pendingRead = nil
        readFailure = nil
        pendingWrite = nil
        writeCompletion = nil
        reader?.cancel()
        writer?.cancel()
        if readSuspended { readSuspended = false; reader?.resume() }
        if writeSuspended { writeSuspended = false; writer?.resume() }
        // Closing stdin cancels the guest. Allow the CLI to report its exit first.
        let child = process
        queue.asyncAfter(deadline: .now() + 1) {
            if child.isRunning { child.terminate() }
        }
        queue.asyncAfter(deadline: .now() + 2) {
            if child.isRunning { kill(child.processIdentifier, SIGKILL) }
        }
    }
}
