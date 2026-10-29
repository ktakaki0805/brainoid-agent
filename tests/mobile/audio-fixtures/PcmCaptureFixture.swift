import Foundation

// Compile with PcmCapture.swift on a host with Swift; contains synthetic samples only.
@main struct PcmCaptureFixture {
    static func main() {
        for (rate, channels) in [(48_000.0, 2), (44_100.0, 1), (16_000.0, 1), (8_000.0, 1)] {
            let capture = PcmCapture(), length = Int(rate)
            let storage = (0..<channels).map { _ -> UnsafeMutablePointer<Float> in
                let pointer = UnsafeMutablePointer<Float>.allocate(capacity: length)
                pointer.initialize(repeating: 0.5, count: length); return pointer
            }
            storage.withUnsafeBufferPointer { pointers in
                // Deliberately split at non-integer output frame boundaries.
                var offset = 0
                while offset < length {
                    let size = min(137, length - offset)
                    let shifted = pointers.map { $0.advanced(by: offset) }
                    shifted.withUnsafeBufferPointer { chunk in
                        precondition(capture.append(channels: chunk.baseAddress!, channelCount: channels, frames: size, sampleRate: rate) == .continuing)
                    }
                    offset += size
                }
            }
            let wav = capture.finish()!
            precondition(wav.count == 32_044 && PcmCapture.validWav(wav))
            precondition(wav[44] == 255 && wav[45] == 63)
            for pointer in storage { pointer.deinitialize(count: length); pointer.deallocate() }
        }
        let capture = PcmCapture(), pointer = UnsafeMutablePointer<Float>.allocate(capacity: 16_000)
        pointer.initialize(repeating: -1, count: 16_000)
        [pointer].withUnsafeBufferPointer { channels in
            for second in 0..<61 {
                let limited = capture.append(channels: channels.baseAddress!, channelCount: 1, frames: 16_000, sampleRate: 16_000)
                precondition(limited == (second >= 59 ? .limit : .continuing))
            }
        }
        let wav = capture.finish()!
        precondition(wav.count == 1_920_044 && PcmCapture.validWav(wav))
        pointer.deinitialize(count: 16_000); pointer.deallocate()
        invalidInputFixtures()
        configurationFixtures()
        print("iOS PCM fixture: conversion/60s, invalid input erasure, active/stale asynchronous configuration notifications passed")
    }
    static func filledCapture() -> PcmCapture {
        let capture = PcmCapture(), pointer = UnsafeMutablePointer<Float>.allocate(capacity: 160)
        pointer.initialize(repeating: 0.5, count: 160)
        defer { pointer.deinitialize(count: 160); pointer.deallocate() }
        [pointer].withUnsafeBufferPointer { channels in
            precondition(capture.append(channels: channels.baseAddress!, channelCount: 1, frames: 160, sampleRate: 16_000) == .continuing)
        }
        return capture
    }
    static func invalidInputFixtures() {
        // Invalid metadata must erase already collected nonzero PCM. Metadata is checked
        // before any channel access, so each supplied pointer remains in bounds.
        let pointer = UnsafeMutablePointer<Float>.allocate(capacity: 160)
        pointer.initialize(repeating: 0.5, count: 160)
        defer { pointer.deinitialize(count: 160); pointer.deallocate() }
        for (rate, channels) in [(48_000.0, 1), (0.0, 1), (Double.nan, 1),
                                 (Double.infinity, 1), (192_001.0, 1), (16_000.0, 0),
                                 (16_000.0, 9), (16_000.0, 2)] {
            let capture = filledCapture()
            [pointer].withUnsafeBufferPointer { storage in
                precondition(capture.append(channels: storage.baseAddress!, channelCount: channels, frames: 160, sampleRate: rate) == .invalid)
                precondition(capture.append(channels: storage.baseAddress!, channelCount: 1, frames: 160, sampleRate: 16_000) == .invalid)
            }
            precondition(capture.isInvalid && capture.finish() == nil)
        }
    }
    static func configurationFixtures() {
        let center = NotificationCenter(), name = Notification.Name("syntheticEngineConfigurationChange")
        let oldEngine = NSObject(), newEngine = NSObject(), unrelatedEngine = NSObject()
        var currentEngine: AnyObject = oldEngine, currentGeneration = 1.0
        var pending = [() -> Void](), stopped = 0
        let oldCapture = filledCapture(), newCapture = filledCapture()
        let oldObserver = CaptureConfigurationObserver(engine: oldEngine, generation: 1, capture: oldCapture,
            notificationName: name, center: center, deliver: { pending.append($0) },
            isCurrent: { currentEngine === $0 && currentGeneration == $1 },
            interrupted: { stopped += 1 })
        center.post(name: name, object: unrelatedEngine)
        precondition(pending.isEmpty && !oldCapture.isInvalid)
        center.post(name: name, object: oldEngine)
        precondition(stopped == 0 && pending.count == 1 && oldCapture.finish() == nil)
        // New engine starts before the old notification reaches the main queue.
        currentEngine = newEngine
        pending.removeFirst()()
        precondition(stopped == 0 && !newCapture.isInvalid)
        // Generation must also match even when a caller presents the same engine identity.
        center.post(name: name, object: oldEngine)
        currentEngine = oldEngine; currentGeneration = 2
        pending.removeFirst()()
        precondition(stopped == 0)
        currentEngine = newEngine
        oldObserver.cancel()
        let newObserver = CaptureConfigurationObserver(engine: newEngine, generation: 2, capture: newCapture,
            notificationName: name, center: center, deliver: { pending.append($0) },
            isCurrent: { currentEngine === $0 && currentGeneration == $1 },
            interrupted: { stopped += 1 })
        center.post(name: name, object: newEngine)
        precondition(stopped == 0 && newCapture.isInvalid && newCapture.finish() == nil)
        pending.removeFirst()()
        precondition(stopped == 1)
        newObserver.cancel()
        center.post(name: name, object: newEngine)
        precondition(pending.isEmpty)
    }
}
