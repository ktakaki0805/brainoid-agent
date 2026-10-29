import AVFoundation
import Capacitor
import UIKit

/// App-local Capacitor 8 bridge. State and AV lifecycle operations run on the main queue.
@objc(BrainoidAudioPlugin)
public final class BrainoidAudioPlugin: CAPPlugin, CAPBridgedPlugin, AVAudioPlayerDelegate {
    public let identifier = "BrainoidAudioPlugin"
    public let jsName = "BrainoidAudio"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "activate", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "permission", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "start", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "finish", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "play", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "stop", returnType: CAPPluginReturnPromise)
    ]
    private var floor = 0.0
    private var active = 0.0
    private var engine: AVAudioEngine?
    private var capture: PcmCapture?
    private var engineObserver: CaptureConfigurationObserver?
    private var player: AVAudioPlayer?
    private var playCall: CAPPluginCall?
    private var recordingDeadline: DispatchWorkItem?
    private var leaseDeadline: DispatchWorkItem?
    private var playDeadline: DispatchWorkItem?
    private var observers = [NSObjectProtocol]()
    private var foreground: Bool { UIApplication.shared.applicationState == .active }
    private var now: Double { Date().timeIntervalSince1970 * 1000 }

    public override func load() {
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: UIApplication.didEnterBackgroundNotification, object: nil, queue: .main) { [weak self] _ in self?.interrupt("background") })
        observers.append(center.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] notice in
            if (notice.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt) == AVAudioSession.InterruptionType.began.rawValue { self?.interrupt("interrupted") }
        })
        observers.append(center.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] notice in
            if (notice.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt) == AVAudioSession.RouteChangeReason.oldDeviceUnavailable.rawValue { self?.interrupt("interrupted") }
        })
        observers.append(center.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: .main) { [weak self] _ in self?.interrupt("error") })
    }
    deinit {
        recordingDeadline?.cancel(); leaseDeadline?.cancel(); playDeadline?.cancel()
        engineObserver?.cancel()
        engine?.stop(); engine?.inputNode.removeTap(onBus: 0); capture?.discard(); player?.stop()
        for observer in observers { NotificationCenter.default.removeObserver(observer) }
    }
    private func valid(_ call: CAPPluginCall) -> Bool {
        foreground && active > floor && call.getDouble("generation") == active
    }
    private func event(_ generation: Double, _ reason: String) {
        notifyListeners("audioStopped", data: ["generation": generation, "reason": reason])
    }
    private func stopRecording() {
        recordingDeadline?.cancel(); recordingDeadline = nil
        if let observer = engineObserver {
            engineObserver = nil; observer.cancel()
        }
        if let old = engine { engine = nil; old.stop(); old.inputNode.removeTap(onBus: 0) }
    }
    private func stopPlayer(_ success: Bool) {
        playDeadline?.cancel(); playDeadline = nil
        player?.stop(); player = nil
        if let call = playCall { playCall = nil; if success { call.resolve() } else { call.reject("Audio stopped") } }
    }
    private func cleanup() {
        stopRecording(); stopPlayer(false); capture?.discard(); capture = nil
        leaseDeadline?.cancel(); leaseDeadline = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
    private func interrupt(_ reason: String) {
        let old = active; floor = max(floor, active); cleanup()
        if old != 0 { event(old, reason) }
    }
    @objc public func activate(_ call: CAPPluginCall) { DispatchQueue.main.async {
        guard let gen = call.getDouble("generation"), gen.isFinite, gen > max(self.floor, self.active), self.foreground,
              let expiry = call.getDouble("expiresAt"), expiry.isFinite, expiry > self.now else { call.reject("Audio context ended"); return }
        self.cleanup(); self.floor = max(self.floor, self.active); self.active = gen
        let deadline = DispatchWorkItem { [weak self] in guard let self = self, self.active == gen else { return }; self.interrupt("interrupted") }
        self.leaseDeadline = deadline
        DispatchQueue.main.asyncAfter(deadline: .now() + (expiry - self.now) / 1000, execute: deadline)
        call.resolve()
    } }
    @objc public func permission(_ call: CAPPluginCall) { DispatchQueue.main.async {
        guard self.valid(call) else { call.reject("Audio context ended"); return }
        AVAudioSession.sharedInstance().requestRecordPermission { granted in DispatchQueue.main.async {
            guard self.valid(call) else { call.reject("Audio context ended"); return }
            call.resolve(["granted": granted])
        } }
    } }
    @objc public func start(_ call: CAPPluginCall) { DispatchQueue.main.async {
        guard self.valid(call), self.engine == nil, self.capture == nil, self.player == nil,
              AVAudioSession.sharedInstance().recordPermission == .granted else { call.reject("Recording unavailable"); return }
        let gen = self.active
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playAndRecord, mode: .measurement, options: [.defaultToSpeaker])
            try session.setActive(true)
            let engine = AVAudioEngine(), capture = PcmCapture()
            let input = engine.inputNode, format = input.outputFormat(forBus: 0)
            guard format.commonFormat == .pcmFormatFloat32, !format.isInterleaved,
                  (1...8).contains(Int(format.channelCount)), (8_000...192_000).contains(format.sampleRate) else { throw AudioFailure.unavailable }
            self.capture = capture; self.engine = engine
            self.engineObserver = CaptureConfigurationObserver(
                engine: engine, generation: gen, capture: capture,
                notificationName: .AVAudioEngineConfigurationChange,
                isCurrent: { [weak self] notifiedEngine, generation in
                    guard let self = self else { return false }
                    return self.active == generation && self.active > self.floor &&
                        self.engine === notifiedEngine
                },
                interrupted: { [weak self] in self?.interrupt("interrupted") }
            )
            input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
                guard let channels = buffer.floatChannelData else {
                    capture.discard()
                    DispatchQueue.main.async {
                        guard let self = self, self.active == gen, self.engine === engine else { return }
                        self.interrupt("error")
                    }
                    return
                }
                let result = capture.append(channels: channels, channelCount: Int(buffer.format.channelCount), frames: Int(buffer.frameLength), sampleRate: buffer.format.sampleRate)
                guard result != .continuing else { return }
                DispatchQueue.main.async {
                    guard let self = self, self.active == gen, self.active > self.floor,
                          self.engine === engine else { return }
                    if result == .invalid || capture.isInvalid { self.interrupt("error") }
                    else { self.stopRecording(); self.event(gen, "limit") }
                }
            }
            try engine.start()
            let deadline = DispatchWorkItem { [weak self] in
                guard let self = self, self.active == gen, self.engine === engine else { return }
                if capture.isInvalid { self.interrupt("error") }
                else { self.stopRecording(); self.event(gen, "limit") }
            }
            self.recordingDeadline = deadline
            DispatchQueue.main.asyncAfter(deadline: .now() + 60, execute: deadline)
            call.resolve()
        } catch { self.interrupt("error"); call.reject("Recording unavailable") }
    } }
    @objc public func finish(_ call: CAPPluginCall) { DispatchQueue.main.async {
        guard self.valid(call), let capture = self.capture else { call.reject("Recording unavailable"); return }
        self.stopRecording(); self.capture = nil
        guard var data = capture.finish() else {
            capture.discard(); self.interrupt("error"); call.reject("Invalid audio"); return
        }
        let encoded = data.base64EncodedString(); data.resetBytes(in: 0..<data.count)
        call.resolve(["wav": encoded])
    } }
    @objc public func play(_ call: CAPPluginCall) { DispatchQueue.main.async {
        guard self.valid(call), self.engine == nil, self.capture == nil, self.player == nil,
              let encoded = call.getString("wav"), encoded.utf8.count <= ((44 + PcmCapture.maxFrames * 2 + 2) / 3) * 4,
              let expiry = call.getDouble("expiresAt"), expiry.isFinite, expiry > self.now,
              var data = Data(base64Encoded: encoded), PcmCapture.validWav(data) else { call.reject("Playback unavailable"); return }
        defer { data.resetBytes(in: 0..<data.count) }
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playback, mode: .spokenAudio)
            try session.setActive(true)
            let player = try AVAudioPlayer(data: data)
            self.player = player; self.playCall = call; player.delegate = self
            let gen = self.active
            let deadline = DispatchWorkItem { [weak self] in
                guard let self = self, self.active == gen, self.player === player else { return }; self.interrupt("interrupted")
            }
            self.playDeadline = deadline
            DispatchQueue.main.asyncAfter(deadline: .now() + min(61, (expiry - self.now) / 1000), execute: deadline)
            guard player.play() else { throw AudioFailure.unavailable }
        } catch { let held = self.playCall === call; self.interrupt("error"); if !held { call.reject("Playback unavailable") } }
    } }
    public func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        guard self.player === player else { return }; if flag { stopPlayer(true) } else { interrupt("error") }
    }
    public func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        if self.player === player { interrupt("error") }
    }
    @objc public func stop(_ call: CAPPluginCall) { DispatchQueue.main.async {
        if let gen = call.getDouble("generation"), gen.isFinite { self.floor = max(self.floor, gen) }
        if self.active <= self.floor { self.cleanup() }; call.resolve()
    } }
    private enum AudioFailure: Error { case unavailable }
}
