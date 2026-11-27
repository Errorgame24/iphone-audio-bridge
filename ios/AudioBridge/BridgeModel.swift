import AVFoundation
import Combine
import CryptoKit
import Foundation
import MediaPlayer
import Security
import UIKit

struct NowPlayingSnapshot: Decodable, Equatable {
    let hasSession: Bool?
    let playing: Bool?
    let title: String?
    let artist: String?
    let album: String?
    let app: String?
    let hostname: String?
    let deviceName: String?
}

struct ServerConfig: Decodable, Equatable {
    let sampleRate: Int
    let channels: Int
    let deviceName: String?
    let hostname: String?
    let micAvailable: Bool?
    let micRate: Int?
    let micChannels: Int?
    let nowPlaying: NowPlayingSnapshot?
}

enum InboundSocketEvent: Equatable {
    case config(ServerConfig)
    case nowPlaying(NowPlayingSnapshot)
    case unknown
}

enum InboundMessageParser {
    private struct Envelope: Decodable {
        let type: String?
    }

    static func parse(_ jsonText: String) -> InboundSocketEvent {
        guard let data = jsonText.data(using: .utf8),
              let env = try? JSONDecoder().decode(Envelope.self, from: data) else {
            return .unknown
        }
        switch env.type {
        case "now-playing":
            if let snap = try? JSONDecoder().decode(NowPlayingSnapshot.self, from: data) {
                return .nowPlaying(snap)
            }
            return .unknown
        case "config", nil:
            if let cfg = try? JSONDecoder().decode(ServerConfig.self, from: data) {
                return .config(cfg)
            }
            return .unknown
        default:
            return .unknown
        }
    }
}

private struct PendingTrust {
    let trust: SecTrust
    let certificate: SecCertificate
    let fingerprint: String
    let host: String
    let pinKey: String
    let completion: (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
}

enum MicSourceMode: String, CaseIterable, Identifiable {
    case builtInKeepStereo = "builtIn"
    case headsetBluetooth = "headset"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .builtInKeepStereo:
            return "手机内置麦 (保真立体声)"
        case .headsetBluetooth:
            return "耳机/系统麦 (蓝牙通话)"
        }
    }
}

final class BridgeModel: NSObject, ObservableObject, URLSessionDelegate, URLSessionTaskDelegate {
    @Published var serverAddress = UserDefaults.standard.string(forKey: "serverAddress") ?? "" {
        didSet { UserDefaults.standard.set(serverAddress, forKey: "serverAddress") }
    }
    @Published var micSourceMode: MicSourceMode = MicSourceMode(
        rawValue: UserDefaults.standard.string(forKey: "micSourceMode") ?? ""
    ) ?? .builtInKeepStereo {
        didSet {
            UserDefaults.standard.set(micSourceMode.rawValue, forKey: "micSourceMode")
            if isMicActive {
                Task { @MainActor in await applyMicSourceChange() }
            }
        }
    }

    @Published private(set) var status = "正在自动扫描局域网电脑，也可手动输入地址连接。"
    @Published private(set) var isConnected = false
    @Published private(set) var isConnecting = false
    @Published private(set) var isReconnecting = false
    @Published private(set) var isMicActive = false
    @Published private(set) var deviceName = ""
    @Published private(set) var serverHostname = ""
    @Published private(set) var sampleRate: Int?
    @Published private(set) var micAvailable = false
    @Published var isTrustPromptPresented = false
    @Published private(set) var certificateFingerprint = ""

    // System Now Playing & AirPods / Control Center state
    @Published private(set) var nowPlayingTitle = "Windows 电脑音频"
    @Published private(set) var nowPlayingArtist = "Nightcord Audio Bridge"
    @Published private(set) var remoteIsPlaying = true

    // Automatic LAN discovery
    @Published private(set) var discoveredServers: [DiscoveredServer] = []
    @Published private(set) var isScanningLAN = false

    private var discovery: ServerDiscovery?
    private var cancellables = Set<AnyCancellable>()
    private var session: URLSession?
    private var audioSocket: URLSessionWebSocketTask?
    private var micSocket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var pingTask: Task<Void, Never>?
    private var reconnectTask: Task<Void, Never>?
    private var micSendTask: Task<Void, Never>?
    private var micContinuation: AsyncStream<Data>.Continuation?
    private var engine: AVAudioEngine?
    private var engineConfigObserver: NSObjectProtocol?
    private var sessionObservers: [NSObjectProtocol] = []
    private var routeRebuildTask: Task<Void, Never>?
    private var pcmBuffer: PCMBuffer?
    private var micTapInstalled = false
    private var serverConfig: ServerConfig?
    private var pendingTrust: PendingTrust?

    private var userWantsConnected = false
    private var userWantsMic = false
    private var remoteCommandsInstalled = false
    private var cachedArtwork: MPMediaItemArtwork?
    private var backgroundTaskID: UIBackgroundTaskIdentifier = .invalid

    override init() {
        super.init()
        Task { @MainActor [weak self] in
            self?.installSystemAudioLifecycleObservers()
        }
    }

    deinit {
        for obs in sessionObservers {
            NotificationCenter.default.removeObserver(obs)
        }
    }

    // MARK: - LAN Discovery

    @MainActor
    func startDiscoveryIfNeeded() {
        if discovery == nil {
            let disc = ServerDiscovery()
            discovery = disc
            disc.$servers
                .receive(on: RunLoop.main)
                .sink { [weak self] list in
                    guard let self else { return }
                    self.discoveredServers = list
                    if self.serverAddress.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                       let first = list.first {
                        self.serverAddress = first.urlString
                    }
                }
                .store(in: &cancellables)

            disc.$isScanning
                .receive(on: RunLoop.main)
                .sink { [weak self] scanning in
                    self?.isScanningLAN = scanning
                }
                .store(in: &cancellables)

            disc.start()
        } else {
            discovery?.scanNow()
        }
    }

    @MainActor
    func refreshDiscovery() {
        startDiscoveryIfNeeded()
    }

    @MainActor
    func connectToDiscovered(_ server: DiscoveredServer) {
        serverAddress = server.urlString
        if isConnected || isConnecting || userWantsConnected {
            disconnect(message: "正在切换到 \(server.displayTitle)…")
        }
        userWantsConnected = true
        Task { await connectToServer(isAutoReconnect: false) }
    }

    // MARK: - Connection & Microphone Controls

    @MainActor
    func toggleConnection() {
        if isConnected || isConnecting || userWantsConnected {
            disconnect(message: "已断开连接。")
        } else {
            userWantsConnected = true
            Task { await connectToServer(isAutoReconnect: false) }
        }
    }

    @MainActor
    func toggleMicrophone() {
        if isMicActive || userWantsMic {
            userWantsMic = false
            stopMicrophone()
        } else {
            userWantsMic = true
            Task { await startMicrophone() }
        }
    }

    @MainActor
    func approveServerCertificate() {
        guard let pendingTrust else { return }
        UserDefaults.standard.set(pendingTrust.fingerprint, forKey: pendingTrust.pinKey)
        let trust = pendingTrust.trust
        let completion = pendingTrust.completion
        self.pendingTrust = nil
        isTrustPromptPresented = false
        completion(.useCredential, URLCredential(trust: trust))
    }

    @MainActor
    func rejectServerCertificate() {
        userWantsConnected = false
        pendingTrust?.completion(.cancelAuthenticationChallenge, nil)
        pendingTrust = nil
        isTrustPromptPresented = false
        status = "未信任服务器证书，连接已取消。"
    }

    // MARK: - Remote Media Transport (AirPods / Control Center / In-App)

    @MainActor
    func sendMediaCommand(_ action: String) {
        let normalized = action.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !normalized.isEmpty else { return }

        switch normalized {
        case "play":
            remoteIsPlaying = true
        case "pause":
            remoteIsPlaying = false
        case "toggle":
            remoteIsPlaying.toggle()
        default:
            break
        }
        updateNowPlayingCenter()

        if let socket = audioSocket {
            let payload: [String: Any] = ["type": "media", "action": normalized]
            if let data = try? JSONSerialization.data(withJSONObject: payload),
               let text = String(data: data, encoding: .utf8) {
                Task {
                    do {
                        try await socket.send(.string(text))
                    } catch {
                        await self.sendMediaCommandViaHTTP(normalized)
                    }
                }
                return
            }
        }

        Task { await sendMediaCommandViaHTTP(normalized) }
    }

    @MainActor
    private func sendMediaCommandViaHTTP(_ action: String) async {
        guard let session, let baseURL = try? validatedBaseURL() else { return }
        var req = URLRequest(url: endpoint("media", baseURL: baseURL, secureWebSocket: false))
        req.httpMethod = "POST"
        req.timeoutInterval = 4
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["action": action])
        _ = try? await session.data(for: req)
    }

    @MainActor
    private func activateSystemMediaSource() {
        UIApplication.shared.beginReceivingRemoteControlEvents()
        installRemoteCommandsIfNeeded()
        setRemoteCommandsEnabled(true)
        updateNowPlayingCenter()
    }

    @MainActor
    private func deactivateSystemMediaSource() {
        setRemoteCommandsEnabled(false)
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
        MPNowPlayingInfoCenter.default().playbackState = .stopped
        UIApplication.shared.endReceivingRemoteControlEvents()
    }

    @MainActor
    private func installRemoteCommandsIfNeeded() {
        guard !remoteCommandsInstalled else { return }
        remoteCommandsInstalled = true

        let center = MPRemoteCommandCenter.shared()
        center.changePlaybackPositionCommand.isEnabled = false
        center.skipForwardCommand.isEnabled = false
        center.skipBackwardCommand.isEnabled = false

        // AirPods single stem press triggers togglePlayPauseCommand
        center.togglePlayPauseCommand.addTarget { [weak self] _ in
            guard let self else { return .commandFailed }
            Task { @MainActor in self.sendMediaCommand("toggle") }
            return .success
        }

        // Control Center / Lock Screen Play button
        center.playCommand.addTarget { [weak self] _ in
            guard let self else { return .commandFailed }
            Task { @MainActor in self.sendMediaCommand("play") }
            return .success
        }

        // Control Center / Lock Screen Pause button:
        // Because AVAudioEngine stays alive at -100 dBFS keepalive in background so iOS never
        // suspends the app, iOS may send pauseCommand even when PC music is already paused.
        // Routing false -> "play" makes Control Center tap 100% reliable in both states!
        center.pauseCommand.addTarget { [weak self] _ in
            guard let self else { return .commandFailed }
            Task { @MainActor in
                let targetAction = self.remoteIsPlaying ? "pause" : "play"
                self.sendMediaCommand(targetAction)
            }
            return .success
        }

        // AirPods double press / Control Center Next
        center.nextTrackCommand.addTarget { [weak self] _ in
            guard let self else { return .commandFailed }
            Task { @MainActor in self.sendMediaCommand("next") }
            return .success
        }

        // AirPods triple press / Control Center Previous
        center.previousTrackCommand.addTarget { [weak self] _ in
            guard let self else { return .commandFailed }
            Task { @MainActor in self.sendMediaCommand("prev") }
            return .success
        }
    }

    @MainActor
    private func setRemoteCommandsEnabled(_ enabled: Bool) {
        let center = MPRemoteCommandCenter.shared()
        center.togglePlayPauseCommand.isEnabled = enabled
        center.playCommand.isEnabled = enabled
        center.pauseCommand.isEnabled = enabled
        center.nextTrackCommand.isEnabled = enabled
        center.previousTrackCommand.isEnabled = enabled
    }

    @MainActor
    private func applyNowPlayingSnapshot(_ snap: NowPlayingSnapshot) {
        if let host = snap.hostname, !host.isEmpty {
            serverHostname = host
        }
        if let dev = snap.deviceName, !dev.isEmpty {
            deviceName = dev
        }
        if let playing = snap.playing {
            remoteIsPlaying = playing
        }

        let rawTitle = (snap.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let rawArtist = (snap.artist ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let rawApp = (snap.app ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

        if !rawTitle.isEmpty {
            nowPlayingTitle = rawTitle
            if !rawArtist.isEmpty {
                nowPlayingArtist = rawArtist
            } else if !rawApp.isEmpty {
                nowPlayingArtist = "\(rawApp) · \(serverHostname.isEmpty ? "电脑音频" : serverHostname)"
            } else {
                nowPlayingArtist = serverHostname.isEmpty ? deviceName : serverHostname
            }
        } else {
            nowPlayingTitle = serverHostname.isEmpty ? "Windows 电脑实时音频" : "\(serverHostname) 音频流"
            nowPlayingArtist = deviceName.isEmpty ? "Nightcord Audio Bridge" : deviceName
        }

        if isConnected || userWantsConnected {
            updateNowPlayingCenter()
        }
    }

    @MainActor
    private func updateNowPlayingCenter() {
        let artwork = resolveNowPlayingArtwork()
        var info: [String: Any] = [
            MPMediaItemPropertyTitle: nowPlayingTitle,
            MPMediaItemPropertyArtist: nowPlayingArtist,
            MPMediaItemPropertyAlbumTitle: serverHostname.isEmpty ? "Nightcord Audio Bridge" : serverHostname,
            MPNowPlayingInfoPropertyIsLiveStream: true,
            MPNowPlayingInfoPropertyPlaybackRate: remoteIsPlaying ? 1.0 : 0.0,
            MPNowPlayingInfoPropertyDefaultPlaybackRate: 1.0,
        ]
        if let artwork {
            info[MPMediaItemPropertyArtwork] = artwork
        }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
        MPNowPlayingInfoCenter.default().playbackState = remoteIsPlaying ? .playing : .paused
    }

    @MainActor
    private func resolveNowPlayingArtwork() -> MPMediaItemArtwork? {
        if let cachedArtwork {
            return cachedArtwork
        }
        let img = UIImage(named: "NightcordArtwork") ?? Self.makeFallbackNightcordImage()
        let art = MPMediaItemArtwork(boundsSize: img.size) { _ in img }
        cachedArtwork = art
        return art
    }

    nonisolated private static func makeFallbackNightcordImage() -> UIImage {
        let size = CGSize(width: 512, height: 512)
        let renderer = UIGraphicsImageRenderer(size: size)
        return renderer.image { ctx in
            UIColor(red: 0.08, green: 0.06, blue: 0.12, alpha: 1.0).setFill()
            ctx.fill(CGRect(origin: .zero, size: size))
            let circle = CGRect(x: 96, y: 96, width: 320, height: 320)
            UIColor(red: 0.53, green: 0.27, blue: 0.60, alpha: 1.0).setStroke()
            let path = UIBezierPath(ovalIn: circle)
            path.lineWidth = 18
            path.stroke()
        }
    }

    // MARK: - Core Connection & Background Keepalive / Auto-Reconnect

    @MainActor
    private func makeConfiguredURLSession() -> URLSession {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 10
        cfg.shouldUseExtendedBackgroundIdleMode = true
        cfg.networkServiceType = .video
        cfg.waitsForConnectivity = false
        return URLSession(configuration: cfg, delegate: self, delegateQueue: nil)
    }

    @MainActor
    private func connectToServer(isAutoReconnect: Bool) async {
        guard !isConnecting else { return }
        isConnecting = true
        if !isAutoReconnect {
            status = "正在连接电脑服务…"
        }

        beginTemporaryBackgroundExecution()
        defer { endTemporaryBackgroundExecution() }

        do {
            let baseURL = try validatedBaseURL()
            let urlSession: URLSession
            if let existing = session {
                urlSession = existing
            } else {
                let created = makeConfiguredURLSession()
                session = created
                urlSession = created
            }

            var configRequest = URLRequest(url: endpoint("config", baseURL: baseURL, secureWebSocket: false))
            configRequest.timeoutInterval = isAutoReconnect ? 5 : 10
            let (data, response) = try await urlSession.data(for: configRequest)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                throw BridgeError.serverResponse
            }

            let config = try JSONDecoder().decode(ServerConfig.self, from: data)
            guard config.sampleRate > 0, (1...2).contains(config.channels) else {
                throw BridgeError.invalidAudioConfig
            }
            let rateChanged = (sampleRate != nil && sampleRate != config.sampleRate)
            serverConfig = config
            deviceName = config.deviceName ?? "电脑音频"
            serverHostname = config.hostname ?? serverHostname
            sampleRate = config.sampleRate
            micAvailable = config.micAvailable ?? false

            let buffer: PCMBuffer
            if let existingBuffer = pcmBuffer, !rateChanged {
                existingBuffer.clear()
                existingBuffer.setKeepAliveDither(true)
                buffer = existingBuffer
            } else {
                let createdBuffer = PCMBuffer(
                    capacity: max(2_048, config.sampleRate / 6),
                    prebufferFrames: max(512, config.sampleRate / 30),
                    keepAliveDither: true
                )
                pcmBuffer = createdBuffer
                buffer = createdBuffer
            }

            // Keep existing running engine alive across background reconnects unless not running or rate changed!
            if engine == nil || engine?.isRunning != true || rateChanged {
                _ = try rebuildAudioEngine(
                    playerSampleRate: config.sampleRate,
                    buffer: buffer,
                    micTarget: nil
                )
            }

            if let snap = config.nowPlaying {
                applyNowPlayingSnapshot(snap)
            } else {
                nowPlayingTitle = serverHostname.isEmpty ? "Windows 电脑实时音频" : "\(serverHostname) 音频流"
                nowPlayingArtist = deviceName
            }

            pingTask?.cancel()
            receiveTask?.cancel()
            audioSocket?.cancel(with: .goingAway, reason: nil)

            let socket = urlSession.webSocketTask(with: endpoint("audio", baseURL: baseURL, secureWebSocket: true))
            audioSocket = socket
            socket.resume()

            isConnected = true
            isConnecting = false
            isReconnecting = false
            activateSystemMediaSource()

            let actualOutRate = Int(AVAudioSession.sharedInstance().sampleRate.rounded())
            status = "已连接 · \(serverHostname.isEmpty ? deviceName : serverHostname) · \(config.sampleRate) Hz"
            sendPlaybackInfo(actualOutRate > 0 ? actualOutRate : config.sampleRate, on: socket)

            let initialChannels = config.channels
            startPingLoop(for: socket)
            receiveTask = Task.detached(priority: .userInitiated) { [weak self] in
                guard let self else { return }
                await self.receiveAudio(from: socket, into: buffer, initialChannels: initialChannels)
            }

            if userWantsMic && !isMicActive && micAvailable {
                await startMicrophone()
            }
        } catch {
            isConnecting = false
            if userWantsConnected {
                // If initial manual connect failed before an engine ever started, show error unless already connected
                if engine == nil && !isAutoReconnect {
                    userWantsConnected = false
                    disconnect(message: "连接失败：\(error.localizedDescription)")
                } else {
                    scheduleAutoReconnect(reason: error.localizedDescription)
                }
            } else {
                disconnect(message: "连接已取消。")
            }
        }
    }

    @MainActor
    private func startPingLoop(for socket: URLSessionWebSocketTask) {
        pingTask?.cancel()
        pingTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 10_000_000_000)
                guard let self, !Task.isCancelled, self.audioSocket === socket else { break }
                let pingFailed = await withCheckedContinuation { (continuation: CheckedContinuation<Bool, Never>) in
                    socket.sendPing { err in
                        continuation.resume(returning: err != nil)
                    }
                }
                if pingFailed, self.audioSocket === socket {
                    socket.cancel(with: .abnormalClosure, reason: nil)
                    break
                }
            }
        }
    }

    @MainActor
    private func handleSocketDrop(socket: URLSessionWebSocketTask, errorDescription: String) {
        guard audioSocket === socket else { return }
        pingTask?.cancel()
        pingTask = nil
        audioSocket?.cancel(with: .goingAway, reason: nil)
        audioSocket = nil

        if isMicActive {
            tearDownMicSocketOnly()
        }

        guard userWantsConnected else {
            disconnect(message: "已断开连接。")
            return
        }

        // CRITICAL: Keep AVAudioEngine running continuously so iOS background audio assertion
        // is NEVER revoked while Wi-Fi roams or PC restarts!
        pcmBuffer?.clear()
        ensureEngineRunningInBackground()
        scheduleAutoReconnect(reason: errorDescription)
    }

    @MainActor
    private func scheduleAutoReconnect(reason: String) {
        guard userWantsConnected else { return }
        if reconnectTask != nil { return }

        isReconnecting = true
        status = "信号波动，后台保活并自动重连中…"
        beginTemporaryBackgroundExecution()

        reconnectTask = Task { @MainActor [weak self] in
            let delaysNs: [UInt64] = [
                700_000_000,
                1_500_000_000,
                2_500_000_000,
                4_000_000_000,
            ]
            var attempt = 0
            while let self, !Task.isCancelled, self.userWantsConnected, self.audioSocket == nil {
                let waitNs = delaysNs[min(attempt, delaysNs.count - 1)]
                attempt += 1
                try? await Task.sleep(nanoseconds: waitNs)
                guard !Task.isCancelled, self.userWantsConnected, self.audioSocket == nil else { break }

                self.ensureEngineRunningInBackground()
                await self.connectToServer(isAutoReconnect: true)
            }
            self?.reconnectTask = nil
            self?.endTemporaryBackgroundExecution()
            let _ = reason
        }
    }

    @MainActor
    private func beginTemporaryBackgroundExecution() {
        guard backgroundTaskID == .invalid else { return }
        backgroundTaskID = UIApplication.shared.beginBackgroundTask(withName: "AudioBridgeKeepAlive") { [weak self] in
            Task { @MainActor [weak self] in
                self?.endTemporaryBackgroundExecution()
            }
        }
    }

    @MainActor
    private func endTemporaryBackgroundExecution() {
        guard backgroundTaskID != .invalid else { return }
        UIApplication.shared.endBackgroundTask(backgroundTaskID)
        backgroundTaskID = .invalid
    }

    // MARK: - System Audio Interruption / Reset / Route Observers

    @MainActor
    private func installSystemAudioLifecycleObservers() {
        guard sessionObservers.isEmpty else { return }
        let nc = NotificationCenter.default
        let audioSession = AVAudioSession.sharedInstance()

        // 1. Phone calls / Siri / alarms / other app audio interruptions
        let interObs = nc.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: audioSession,
            queue: nil
        ) { [weak self] note in
            let typeRaw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            Task { @MainActor [weak self] in
                guard let self, self.userWantsConnected, let typeRaw,
                      let type = AVAudioSession.InterruptionType(rawValue: typeRaw) else { return }
                switch type {
                case .began:
                    self.beginTemporaryBackgroundExecution()
                    self.status = "音频被系统通话/提示临时打断，等待自动恢复…"
                case .ended:
                    try? AVAudioSession.sharedInstance().setActive(true)
                    self.ensureEngineRunningInBackground()
                    self.updateNowPlayingCenter()
                    if let host = self.serverHostname.isEmpty ? Optional(self.deviceName) : Optional(self.serverHostname),
                       let rate = self.sampleRate {
                        self.status = "已连接 · \(host) · \(rate) Hz"
                    }
                    self.endTemporaryBackgroundExecution()
                @unknown default:
                    break
                }
            }
        }
        sessionObservers.append(interObs)

        // 2. CoreAudio mediaserverd reset
        let resetObs = nc.addObserver(
            forName: AVAudioSession.mediaServicesWereResetNotification,
            object: audioSession,
            queue: nil
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.remoteCommandsInstalled = false
                guard self.userWantsConnected else { return }
                self.teardownEngineOnly()
                self.ensureEngineRunningInBackground()
                self.activateSystemMediaSource()
            }
        }
        sessionObservers.append(resetObs)

        // 3. AirPods / Bluetooth route changes (e.g. taking AirPods out of ears)
        let routeObs = nc.addObserver(
            forName: AVAudioSession.routeChangeNotification,
            object: audioSession,
            queue: nil
        ) { [weak self] note in
            let reasonRaw = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
            Task { @MainActor [weak self] in
                guard let self, self.userWantsConnected, let reasonRaw,
                      let reason = AVAudioSession.RouteChangeReason(rawValue: reasonRaw) else { return }
                if reason == .oldDeviceUnavailable {
                    // Pause PC music automatically when headphones disconnect / ear detection triggers,
                    // while keeping iOS AVAudioEngine alive so background execution is preserved!
                    if self.remoteIsPlaying {
                        self.sendMediaCommand("pause")
                    }
                    self.scheduleRouteRecovery()
                } else if reason == .newDeviceAvailable {
                    self.scheduleRouteRecovery()
                }
            }
        }
        sessionObservers.append(routeObs)

        // 4. Foreground return health check
        let fgObs = nc.addObserver(
            forName: UIApplication.willEnterForegroundNotification,
            object: nil,
            queue: nil
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self else { return }
                if self.userWantsConnected {
                    self.ensureEngineRunningInBackground()
                    self.updateNowPlayingCenter()
                    if self.audioSocket == nil && !self.isConnecting {
                        self.reconnectTask?.cancel()
                        self.reconnectTask = nil
                        await self.connectToServer(isAutoReconnect: true)
                    }
                } else {
                    self.discovery?.scanNow()
                }
            }
        }
        sessionObservers.append(fgObs)
    }

    @MainActor
    private func ensureEngineRunningInBackground() {
        guard userWantsConnected,
              let config = serverConfig,
              let buffer = pcmBuffer else { return }

        if let engine, engine.isRunning {
            return
        }

        do {
            try AVAudioSession.sharedInstance().setActive(true)
            if let engine {
                try engine.start()
                return
            }
        } catch {
            // Fall through to full engine rebuild
        }

        _ = try? rebuildAudioEngine(
            playerSampleRate: config.sampleRate,
            buffer: buffer,
            micTarget: nil
        )
    }

    // MARK: - URL Validation & Audio Engine Graph

    @MainActor
    private func validatedBaseURL() throws -> URL {
        var raw = serverAddress.trimmingCharacters(in: .whitespacesAndNewlines)
        if !raw.isEmpty && !raw.contains("://") {
            raw = "https://\(raw)"
            serverAddress = raw
        }
        guard let components = URLComponents(string: raw),
              components.scheme?.lowercased() == "https",
              let host = components.host, !host.isEmpty,
              components.user == nil, components.password == nil,
              components.path.isEmpty || components.path == "/",
              components.query == nil, components.fragment == nil,
              let url = components.url else {
            throw BridgeError.invalidAddress
        }
        _ = host
        return url
    }

    private func endpoint(_ path: String, baseURL: URL, secureWebSocket: Bool) -> URL {
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        components.scheme = secureWebSocket ? "wss" : "https"
        components.path = "/\(path)"
        components.query = nil
        components.fragment = nil
        return components.url!
    }

    @MainActor
    private func teardownEngineOnly() {
        if let engineConfigObserver {
            NotificationCenter.default.removeObserver(engineConfigObserver)
            self.engineConfigObserver = nil
        }
        if micTapInstalled {
            engine?.inputNode.removeTap(onBus: 0)
            micTapInstalled = false
        }
        engine?.stop()
        engine = nil
    }

    nonisolated private static func makePlaybackSourceNode(
        format: AVAudioFormat,
        buffer: PCMBuffer
    ) -> AVAudioSourceNode {
        AVAudioSourceNode(format: format) { _, _, frameCount, audioBufferList -> OSStatus in
            let buffers = UnsafeMutableAudioBufferListPointer(audioBufferList)
            guard buffers.count >= 2,
                  let left = buffers[0].mData?.assumingMemoryBound(to: Float.self),
                  let right = buffers[1].mData?.assumingMemoryBound(to: Float.self) else {
                return noErr
            }
            buffer.render(left: left, right: right, frameCount: Int(frameCount))
            return noErr
        }
    }

    nonisolated private static func installMicrophoneTap(
        on input: AVAudioInputNode,
        targetRate: Int,
        targetChannels: Int,
        continuation: AsyncStream<Data>.Continuation
    ) {
        var cachedConverter: AVAudioConverter?
        var cachedSourceRate: Double = 0
        var cachedSourceChannels: AVAudioChannelCount = 0

        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: nil) { incoming, _ in
            guard incoming.frameLength > 0 else { return }
            let srcFormat = incoming.format
            guard srcFormat.sampleRate > 0, srcFormat.channelCount > 0 else { return }

            let pcmBuf: AVAudioPCMBuffer
            if abs(srcFormat.sampleRate - Double(targetRate)) > 0.5 {
                if cachedConverter == nil
                    || abs(cachedSourceRate - srcFormat.sampleRate) > 0.5
                    || cachedSourceChannels != srcFormat.channelCount {
                    guard let dstFormat = AVAudioFormat(
                        standardFormatWithSampleRate: Double(targetRate),
                        channels: srcFormat.channelCount
                    ) else { return }
                    cachedConverter = AVAudioConverter(from: srcFormat, to: dstFormat)
                    cachedSourceRate = srcFormat.sampleRate
                    cachedSourceChannels = srcFormat.channelCount
                }
                guard let converter = cachedConverter else { return }
                let dstFormat = converter.outputFormat
                let ratio = dstFormat.sampleRate / srcFormat.sampleRate
                let cap = AVAudioFrameCount(Double(incoming.frameLength) * ratio + 64)
                guard let converted = AVAudioPCMBuffer(pcmFormat: dstFormat, frameCapacity: max(64, cap)) else {
                    return
                }
                var providedInput = false
                var conversionError: NSError?
                let res = converter.convert(to: converted, error: &conversionError) { _, inputStatus in
                    if providedInput {
                        inputStatus.pointee = .noDataNow
                        return nil
                    }
                    providedInput = true
                    inputStatus.pointee = .haveData
                    return incoming
                }
                guard res != .error, converted.frameLength > 0 else { return }
                pcmBuf = converted
            } else {
                pcmBuf = incoming
            }

            guard let floatChannels = pcmBuf.floatChannelData else { return }
            let leftPtr = UnsafePointer(floatChannels[0])
            let rightPtr: UnsafePointer<Float>? = pcmBuf.format.channelCount > 1
                ? UnsafePointer(floatChannels[1])
                : nil
            let packet = PCMMicEncoder.encodeInterleavedInt16(
                left: leftPtr,
                right: rightPtr,
                frameCount: Int(pcmBuf.frameLength),
                targetChannels: targetChannels
            )
            if !packet.isEmpty {
                continuation.yield(packet)
            }
        }
    }

    @MainActor
    private func rebuildAudioEngine(
        playerSampleRate: Int,
        buffer: PCMBuffer,
        micTarget: (rate: Int, channels: Int, continuation: AsyncStream<Data>.Continuation)?
    ) throws -> Int? {
        teardownEngineOnly()

        let audioSession = AVAudioSession.sharedInstance()
        if micTarget != nil {
            let options: AVAudioSession.CategoryOptions = micSourceMode == .builtInKeepStereo
                ? [.defaultToSpeaker, .allowBluetoothA2DP]
                : [.defaultToSpeaker, .allowBluetooth, .allowBluetoothA2DP]
            try audioSession.setCategory(.playAndRecord, mode: .default, options: options)
            try? audioSession.setPreferredSampleRate(Double(playerSampleRate))
            try? audioSession.setPreferredIOBufferDuration(0.01)
            try audioSession.setActive(true)

            if micSourceMode == .builtInKeepStereo,
               let builtIn = audioSession.availableInputs?.first(where: { $0.portType == .builtInMic }) {
                try? audioSession.setPreferredInput(builtIn)
            } else {
                try? audioSession.setPreferredInput(nil)
            }
        } else {
            try? audioSession.setPreferredInput(nil)
            try audioSession.setCategory(.playback, mode: .default)
            try? audioSession.setPreferredSampleRate(Double(playerSampleRate))
            try? audioSession.setPreferredIOBufferDuration(0.01)
            try audioSession.setActive(true)
        }

        let audioEngine = AVAudioEngine()
        var detectedInputRate: Int?

        if let micTarget {
            guard audioSession.isInputAvailable else {
                throw BridgeError.microphoneUnavailable("系统当前无可用音频输入设备")
            }
            let input = audioEngine.inputNode
            let hwFormat = input.inputFormat(forBus: 0)
            let outFormat = input.outputFormat(forBus: 0)
            let activeRate = outFormat.sampleRate > 0 ? outFormat.sampleRate : hwFormat.sampleRate
            let activeChannels = outFormat.channelCount > 0 ? outFormat.channelCount : hwFormat.channelCount
            guard hwFormat.sampleRate > 0, hwFormat.channelCount > 0,
                  outFormat.sampleRate > 0, outFormat.channelCount > 0 else {
                throw BridgeError.microphoneUnavailable(
                    "hw=\(Int(hwFormat.sampleRate))Hz/\(hwFormat.channelCount)ch, bus=\(Int(outFormat.sampleRate))Hz/\(outFormat.channelCount)ch"
                )
            }

            Self.installMicrophoneTap(
                on: input,
                targetRate: micTarget.rate,
                targetChannels: micTarget.channels,
                continuation: micTarget.continuation
            )
            micTapInstalled = true
            detectedInputRate = Int(activeRate.rounded())
            _ = activeChannels
        }

        let playFormat = AVAudioFormat(standardFormatWithSampleRate: Double(playerSampleRate), channels: 2)!
        let source = Self.makePlaybackSourceNode(format: playFormat, buffer: buffer)
        audioEngine.attach(source)
        audioEngine.connect(source, to: audioEngine.mainMixerNode, format: playFormat)

        audioEngine.prepare()
        try audioEngine.start()
        engine = audioEngine

        engineConfigObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: audioEngine,
            queue: nil
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.scheduleRouteRecovery()
            }
        }

        return detectedInputRate
    }

    @MainActor
    private func rebuildMicEngineWithRetry(
        playerSampleRate: Int,
        buffer: PCMBuffer,
        micTarget: (rate: Int, channels: Int, continuation: AsyncStream<Data>.Continuation)
    ) async throws -> Int {
        var lastError: Error?
        for attempt in 0..<6 {
            do {
                if let rate = try rebuildAudioEngine(
                    playerSampleRate: playerSampleRate,
                    buffer: buffer,
                    micTarget: micTarget
                ) {
                    return rate
                }
                return micTarget.rate
            } catch {
                lastError = error
                if attempt < 5 {
                    try? await Task.sleep(nanoseconds: 150_000_000)
                }
            }
        }
        throw lastError ?? BridgeError.microphoneUnavailable("蓝牙/麦克风路由初始化超时")
    }

    @MainActor
    private func applyMicSourceChange() async {
        guard isConnected, isMicActive,
              let config = serverConfig,
              let targetRate = config.micRate,
              let targetChannels = config.micChannels,
              let buffer = pcmBuffer,
              let continuation = micContinuation else { return }
        do {
            let inputRate = try await rebuildMicEngineWithRetry(
                playerSampleRate: config.sampleRate,
                buffer: buffer,
                micTarget: (targetRate, targetChannels, continuation)
            )
            if let micSocket {
                try? await sendMicrophoneInfo(inputRate: inputRate, targetRate: targetRate, on: micSocket)
            }
            if let audioSocket {
                let outRate = Int(AVAudioSession.sharedInstance().sampleRate.rounded())
                if outRate > 0 {
                    sendPlaybackInfo(outRate, on: audioSocket)
                }
            }
            let routeName = AVAudioSession.sharedInstance().currentRoute.inputs.first?.portName ?? micSourceMode.title
            status = "麦克风已回传 (\(routeName)) · \(inputRate) Hz → \(targetRate) Hz"
        } catch {
            status = "切换麦克风音源失败：\(error.localizedDescription)"
        }
    }

    @MainActor
    private func scheduleRouteRecovery() {
        routeRebuildTask?.cancel()
        routeRebuildTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 300_000_000)
            guard let self, !Task.isCancelled, self.userWantsConnected,
                  let config = self.serverConfig,
                  let buffer = self.pcmBuffer else { return }
            if let engine = self.engine, engine.isRunning {
                return
            }
            do {
                if self.isMicActive,
                   let targetRate = config.micRate,
                   let targetChannels = config.micChannels,
                   let continuation = self.micContinuation {
                    let inRate = try await self.rebuildMicEngineWithRetry(
                        playerSampleRate: config.sampleRate,
                        buffer: buffer,
                        micTarget: (targetRate, targetChannels, continuation)
                    )
                    if let micSocket = self.micSocket {
                        try? await self.sendMicrophoneInfo(
                            inputRate: inRate,
                            targetRate: targetRate,
                            on: micSocket
                        )
                    }
                    let routeName = AVAudioSession.sharedInstance().currentRoute.inputs.first?.portName ?? self.micSourceMode.title
                    self.status = "麦克风已回传 (\(routeName)) · \(inRate) Hz → \(targetRate) Hz"
                } else {
                    _ = try self.rebuildAudioEngine(
                        playerSampleRate: config.sampleRate,
                        buffer: buffer,
                        micTarget: nil
                    )
                }
                self.updateNowPlayingCenter()
                if let audioSocket = self.audioSocket {
                    let outRate = Int(AVAudioSession.sharedInstance().sampleRate.rounded())
                    if outRate > 0 {
                        self.sendPlaybackInfo(outRate, on: audioSocket)
                    }
                }
            } catch {
                self.status = "音频路由变化后恢复失败：\(error.localizedDescription)"
            }
        }
    }

    private func receiveAudio(
        from socket: URLSessionWebSocketTask,
        into buffer: PCMBuffer,
        initialChannels: Int
    ) async {
        var currentChannels = initialChannels
        while !Task.isCancelled {
            do {
                switch try await socket.receive() {
                case .data(let data):
                    buffer.append(data, channels: currentChannels)
                case .string(let message):
                    switch InboundMessageParser.parse(message) {
                    case .nowPlaying(let snap):
                        await MainActor.run {
                            self.applyNowPlayingSnapshot(snap)
                        }
                    case .config(let updated):
                        if (1...2).contains(updated.channels) {
                            currentChannels = updated.channels
                        }
                        await MainActor.run {
                            self.handleLiveConfigUpdate(updated, buffer: buffer)
                        }
                    case .unknown:
                        break
                    }
                @unknown default:
                    break
                }
            } catch {
                guard !Task.isCancelled else { break }
                await MainActor.run {
                    self.handleSocketDrop(socket: socket, errorDescription: error.localizedDescription)
                }
                break
            }
        }
    }

    @MainActor
    private func handleLiveConfigUpdate(_ updated: ServerConfig, buffer: PCMBuffer) {
        deviceName = updated.deviceName ?? deviceName
        if let h = updated.hostname, !h.isEmpty {
            serverHostname = h
        }
        if let snap = updated.nowPlaying {
            applyNowPlayingSnapshot(snap)
        }
        if updated.sampleRate > 0, updated.sampleRate != sampleRate {
            sampleRate = updated.sampleRate
            buffer.clear()
            _ = try? rebuildAudioEngine(
                playerSampleRate: updated.sampleRate,
                buffer: buffer,
                micTarget: nil
            )
            status = "已连接 · \(serverHostname.isEmpty ? deviceName : serverHostname) · \(updated.sampleRate) Hz"
        }
        updateNowPlayingCenter()
    }

    @MainActor
    private func sendPlaybackInfo(_ rate: Int, on socket: URLSessionWebSocketTask) {
        let payload = ["type": "audio-info", "playbackSampleRate": rate] as [String: Any]
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let text = String(data: data, encoding: .utf8) else { return }
        Task { try? await socket.send(.string(text)) }
    }

    // MARK: - Microphone Streaming

    @MainActor
    private func startMicrophone() async {
        guard isConnected, let config = serverConfig,
              config.micAvailable == true,
              let targetRate = config.micRate, targetRate > 0,
              let targetChannels = config.micChannels, (1...2).contains(targetChannels),
              let session,
              let buffer = pcmBuffer else {
            userWantsMic = false
            status = "电脑端未检测到 VB-Cable，无法回传麦克风。"
            return
        }

        let audioSession = AVAudioSession.sharedInstance()
        let allowed = await withCheckedContinuation { continuation in
            audioSession.requestRecordPermission { granted in continuation.resume(returning: granted) }
        }
        guard allowed else {
            userWantsMic = false
            status = "请在 iPhone 设置中允许 Audio Bridge 使用麦克风。"
            return
        }

        var continuation: AsyncStream<Data>.Continuation!
        let stream = AsyncStream<Data>(bufferingPolicy: .bufferingNewest(8)) {
            continuation = $0
        }
        micContinuation = continuation

        do {
            let inputRate = try await rebuildMicEngineWithRetry(
                playerSampleRate: config.sampleRate,
                buffer: buffer,
                micTarget: (targetRate, targetChannels, continuation)
            )

            let baseURL = try validatedBaseURL()
            let socket = session.webSocketTask(with: endpoint("mic", baseURL: baseURL, secureWebSocket: true))
            micSocket = socket
            socket.resume()
            try await sendMicrophoneInfo(inputRate: inputRate, targetRate: targetRate, on: socket)

            if let audioSocket {
                let actualOutRate = Int(AVAudioSession.sharedInstance().sampleRate.rounded())
                if actualOutRate > 0 {
                    sendPlaybackInfo(actualOutRate, on: audioSocket)
                }
            }

            micSendTask = Task {
                for await packet in stream {
                    guard !Task.isCancelled else { break }
                    do { try await socket.send(.data(packet)) }
                    catch {
                        await MainActor.run { self.status = "麦克风回传中断，等待网络恢复…" }
                        break
                    }
                }
            }

            isMicActive = true
            updateNowPlayingCenter()
            let routeName = AVAudioSession.sharedInstance().currentRoute.inputs.first?.portName ?? micSourceMode.title
            status = "麦克风已回传 (\(routeName)) · \(inputRate) Hz → \(targetRate) Hz"
        } catch {
            let errorMessage = error.localizedDescription
            stopMicrophone()
            status = "无法启动麦克风：\(errorMessage)"
        }
    }

    @MainActor
    private func sendMicrophoneInfo(inputRate: Int, targetRate: Int, on socket: URLSessionWebSocketTask) async throws {
        let payload = [
            "type": "audio-info",
            "micInputSampleRate": inputRate,
            "micContextSampleRate": targetRate
        ] as [String: Any]
        let data = try JSONSerialization.data(withJSONObject: payload)
        try await socket.send(.string(String(decoding: data, as: UTF8.self)))
    }

    @MainActor
    private func tearDownMicSocketOnly() {
        micContinuation?.finish()
        micContinuation = nil
        micSendTask?.cancel()
        micSendTask = nil
        micSocket?.cancel(with: .normalClosure, reason: nil)
        micSocket = nil
        isMicActive = false
    }

    @MainActor
    private func stopMicrophone() {
        routeRebuildTask?.cancel()
        routeRebuildTask = nil
        tearDownMicSocketOnly()
        if userWantsConnected, let config = serverConfig, let buffer = pcmBuffer {
            do {
                _ = try rebuildAudioEngine(
                    playerSampleRate: config.sampleRate,
                    buffer: buffer,
                    micTarget: nil
                )
                updateNowPlayingCenter()
                if let audioSocket {
                    let outRate = Int(AVAudioSession.sharedInstance().sampleRate.rounded())
                    if outRate > 0 {
                        sendPlaybackInfo(outRate, on: audioSocket)
                    }
                }
                status = "已连接 · \(serverHostname.isEmpty ? deviceName : serverHostname) · \(sampleRate ?? config.sampleRate) Hz"
            } catch {
                status = "麦克风已停止；音频路由恢复失败：\(error.localizedDescription)"
            }
        } else {
            teardownEngineOnly()
        }
    }

    @MainActor
    private func disconnect(message: String) {
        userWantsConnected = false
        userWantsMic = false
        isReconnecting = false

        reconnectTask?.cancel()
        reconnectTask = nil
        pingTask?.cancel()
        pingTask = nil
        routeRebuildTask?.cancel()
        routeRebuildTask = nil

        tearDownMicSocketOnly()

        receiveTask?.cancel()
        receiveTask = nil
        audioSocket?.cancel(with: .normalClosure, reason: nil)
        audioSocket = nil

        deactivateSystemMediaSource()
        teardownEngineOnly()
        pcmBuffer = nil

        session?.invalidateAndCancel()
        session = nil
        endTemporaryBackgroundExecution()

        isConnected = false
        isConnecting = false
        deviceName = ""
        serverHostname = ""
        sampleRate = nil
        micAvailable = false
        nowPlayingTitle = "Windows 电脑音频"
        nowPlayingArtist = "Nightcord Audio Bridge"
        remoteIsPlaying = true
        status = message
    }

    // MARK: - TLS Certificate Pinning

    private func leafCertificate(from trust: SecTrust) -> SecCertificate? {
        guard let chain = SecTrustCopyCertificateChain(trust),
              CFArrayGetCount(chain) > 0,
              let rawPtr = CFArrayGetValueAtIndex(chain, 0) else {
            return nil
        }
        return unsafeBitCast(rawPtr, to: SecCertificate.self)
    }

    private func handleTrustChallenge(
        _ challenge: URLAuthenticationChallenge,
        completion: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let certificate = leafCertificate(from: trust) else {
            completion(.performDefaultHandling, nil)
            return
        }

        let host = challenge.protectionSpace.host
        let port = challenge.protectionSpace.port
        let pinKey = "trusted-certificate-\(host):\(port)"
        let certificateData = SecCertificateCopyData(certificate) as Data
        guard !certificateData.isEmpty else {
            completion(.cancelAuthenticationChallenge, nil)
            return
        }
        let fingerprint = SHA256.hash(data: certificateData)
            .map { String(format: "%02x", $0) }
            .joined(separator: ":")

        if UserDefaults.standard.string(forKey: pinKey) == fingerprint {
            completion(.useCredential, URLCredential(trust: trust))
            return
        }

        DispatchQueue.main.async {
            self.pendingTrust?.completion(.cancelAuthenticationChallenge, nil)
            self.pendingTrust = PendingTrust(
                trust: trust,
                certificate: certificate,
                fingerprint: fingerprint,
                host: host,
                pinKey: pinKey,
                completion: completion
            )
            self.certificateFingerprint = fingerprint
            self.isTrustPromptPresented = true
        }
    }

    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        handleTrustChallenge(challenge, completion: completionHandler)
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        handleTrustChallenge(challenge, completion: completionHandler)
    }
}

private enum BridgeError: LocalizedError {
    case invalidAddress
    case invalidAudioConfig
    case microphoneUnavailable(String)
    case serverResponse

    var errorDescription: String? {
        switch self {
        case .invalidAddress:
            return "请输入电脑控制台显示的 HTTPS 根地址，例如 https://192.168.1.20:8443。"
        case .invalidAudioConfig:
            return "电脑服务返回了不支持的音频格式。"
        case .microphoneUnavailable(let detail):
            return "无法获取 iPhone 麦克风音频格式（\(detail)）。"
        case .serverResponse:
            return "电脑服务未返回有效配置，请确认地址和服务状态。"
        }
    }
}