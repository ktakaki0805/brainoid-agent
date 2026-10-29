import Foundation

/// Foundation-only notification boundary, shared by the AVAudioEngine bridge and its fixture.
final class CaptureConfigurationObserver {
    private let center: NotificationCenter
    private var token: NSObjectProtocol?

    init(engine: AnyObject, generation: Double, capture: PcmCapture,
         notificationName: Notification.Name, center: NotificationCenter = .default,
         deliver: @escaping (@escaping () -> Void) -> Void = { action in DispatchQueue.main.async(execute: action) },
         isCurrent: @escaping (AnyObject, Double) -> Bool,
         interrupted: @escaping () -> Void) {
        self.center = center
        token = center.addObserver(forName: notificationName, object: engine, queue: nil) { [weak engine] _ in
            // The OS posts on an internal queue. Invalidate PCM before any queued finish;
            // never stop/deallocate the engine synchronously in this callback.
            capture.discard()
            deliver {
                guard let engine = engine, isCurrent(engine, generation) else { return }
                interrupted()
            }
        }
    }
    func cancel() {
        if let token = token { self.token = nil; center.removeObserver(token) }
    }
    deinit { cancel() }
}
