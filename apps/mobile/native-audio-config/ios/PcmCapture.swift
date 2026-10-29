import Foundation

/// Streaming area resampler: bounded mono PCM16, no file-backed buffer.
final class PcmCapture {
    enum AppendResult: Equatable { case continuing, limit, invalid }
    static let maxFrames = 960_000
    private let lock = NSLock()
    private var pcm = [Int16]()
    private var area = 0.0
    private var weight = 0.0
    private var rate = 0.0
    private var channelCount = 0
    private var result: AppendResult = .continuing
    init() { pcm.reserveCapacity(Self.maxFrames) }

    func append(channels: UnsafePointer<UnsafeMutablePointer<Float>>, channelCount: Int,
                frames: Int, sampleRate: Double) -> AppendResult {
        lock.lock(); defer { lock.unlock() }
        guard result != .invalid else { return .invalid }
        guard sampleRate.isFinite, (8_000...192_000).contains(sampleRate), frames >= 0,
              (1...8).contains(channelCount), rate == 0 || rate == sampleRate,
              self.channelCount == 0 || self.channelCount == channelCount else {
            invalidateLocked(); return .invalid
        }
        guard result != .limit else { return .limit }
        rate = sampleRate; self.channelCount = channelCount
        let width = sampleRate / 16_000
        for frame in 0..<frames {
            var mono = 0.0
            for channel in 0..<channelCount {
                let sample = Double(channels[channel][frame])
                mono += sample.isFinite ? min(1, max(-1, sample)) : 0
            }
            mono /= Double(channelCount)
            var remaining = 1.0
            while remaining > 0.0000001 {
                let take = min(remaining, width - weight)
                area += mono * take; weight += take; remaining -= take
                if weight >= width - 0.0000001 {
                    let value = min(1, max(-1, area / width))
                    pcm.append(Int16((value * (value < 0 ? 32768 : 32767)).rounded(.towardZero)))
                    area = 0; weight = 0
                    if pcm.count == Self.maxFrames { result = .limit; return .limit }
                }
            }
        }
        return .continuing
    }
    var isInvalid: Bool {
        lock.lock(); defer { lock.unlock() }
        return result == .invalid
    }
    func finish() -> Data? {
        lock.lock(); defer { lock.unlock() }
        guard result != .invalid, !pcm.isEmpty else { invalidateLocked(); return nil }
        var bytes = Data()
        func text(_ value: String) { bytes.append(contentsOf: value.utf8) }
        func u16(_ value: UInt16) { bytes.append(UInt8(value & 255)); bytes.append(UInt8(value >> 8)) }
        func u32(_ value: UInt32) { u16(UInt16(value & 65535)); u16(UInt16(value >> 16)) }
        text("RIFF"); u32(UInt32(36 + pcm.count * 2)); text("WAVEfmt "); u32(16)
        u16(1); u16(1); u32(16_000); u32(32_000); u16(2); u16(16)
        text("data"); u32(UInt32(pcm.count * 2))
        for value in pcm { u16(UInt16(bitPattern: value)) }
        invalidateLocked()
        return bytes
    }
    func discard() {
        lock.lock(); defer { lock.unlock() }
        invalidateLocked()
    }
    /// Caller holds lock. Invalid state is terminal and cannot expose a partial recording.
    private func invalidateLocked() {
        result = .invalid
        for index in pcm.indices { pcm[index] = 0 }; pcm.removeAll(keepingCapacity: false)
        area = 0; weight = 0; rate = 0; channelCount = 0
    }
    static func validWav(_ data: Data) -> Bool {
        guard data.count >= 46, data.count <= 44 + maxFrames * 2, data.count % 2 == 0 else { return false }
        func u16(_ i: Int) -> UInt32 { UInt32(data[i]) | UInt32(data[i + 1]) << 8 }
        func u32(_ i: Int) -> UInt32 { u16(i) | u16(i + 2) << 16 }
        return u32(0) == 0x46464952 && u32(4) == UInt32(data.count - 8) && u32(8) == 0x45564157 &&
            u32(12) == 0x20746d66 && u32(16) == 16 && u16(20) == 1 && u16(22) == 1 &&
            u32(24) == 16_000 && u32(28) == 32_000 && u16(32) == 2 && u16(34) == 16 &&
            u32(36) == 0x61746164 && u32(40) == UInt32(data.count - 44)
    }
}
