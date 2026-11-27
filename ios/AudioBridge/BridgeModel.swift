import AVFoundation
import Combine
import CryptoKit
import Foundation
import Security

private struct ServerConfig: Decodable {
    let sampleRate: Int
    let channels: Int
    let deviceName: String?
    let micAvailable: Bool?
    let micRate: Int?
    let micChannels: Int?
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
    @Published private(set) var status = "输入电脑上显示的 HTTPS 地址，然后连接。"
    @Published private(set) var isConnected = false
    @Published private(set) var isConnecting = false
    @Published private(set) var isMicActive = false
    @Published private(set) var deviceName = ""
    @Published private(set) var sampleRate: Int?
    @Published private(set) var micAvailable = false
    @Published var isTrustPromptPresented = false
    @Published private(set) var certificateFingerprint = ""

    private var session: URLSession?
    private var audioSocket: URLSessionWebSocketTask?
    private var micSocket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var micSendTask: Task<Void, Never>?
    private var micContinuation: AsyncStream<Data>.Continuation?
    private var engine: AVAudioEngine?
    private var engineConfigObserver: NSObjectProtocol?
    private var routeRebuildTask: Task<Void, Never>?
    private var pcmBuffer: PCMBuffer?
    private var micTapInstalled = false
    private var serverConfig: ServerConfig?
    private var pendingTrust: PendingTrust?

    @MainActor
    func toggleConnection() {
        if isConnected || isConnecting {
            disconnect(message: "已断开连接。")
        } else {
            Task { await connectToServer() }
        }
    }

    @MainActor
    func toggleMicrophone() {
        if isMicActive {
            stopMicrophone()
        } else {
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
        pendingTrust?.completion(.cancelAuthenticationChallenge, nil)
        pendingTrust = nil
        isTrustPromptPresented = false
        status = "未信任服务器证书，连接已取消。"
    }

    @MainActor
    private func connectToServer() async {
        guard !isConnecting else { return }
        isConnecting = true
        status = "正在连接电脑服务…"

        do {
            let baseURL = try validatedBaseURL()
            let urlSession = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
            session = urlSession

            var configRequest = URLRequest(url: endpoint("config", baseURL: baseURL, secureWebSocket: false))
            configRequest.timeoutInterval = 10
            let (data, response) = try await urlSession.data(for: configRequest)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                throw BridgeError.serverResponse
            }

            let config = try JSONDecoder().decode(ServerConfig.self, from: data)
            guard config.sampleRate > 0, (1...2).contains(config.channels) else {
                throw BridgeError.invalidAudioConfig
            }
            serverConfig = config
            deviceName = config.deviceName ?? "电脑音频"
            sampleRate = config.sampleRate
            micAvailable = config.micAvailable ?? false

            let buffer = PCMBuffer(
                capacity: max(2_048, config.sampleRate / 6),
                prebufferFrames: max(512, config.sampleRate / 30)
            )
            pcmBuffer = buffer
            _ = try rebuildAudioEngine(
                playerSampleRate: config.sampleRate,
                buffer: buffer,
                micTarget: nil
            )

            let socket = urlSession.webSocketTask(with: endpoint("audio", baseURL: baseURL, secureWebSocket: true))
            audioSocket = socket
            socket.resume()
            isConnected = true
            isConnecting = false
            let actualOutRate = Int(AVAudioSession.sharedInstance().sampleRate.rounded())
            status = "已连接 · \(deviceName) · \(config.sampleRate) Hz"
            sendPlaybackInfo(actualOutRate > 0 ? actualOutRate : config.sampleRate, on: socket)
            let initialChannels = config.channels
            receiveTask = Task.detached(priority: .userInitiated) { [weak self] in
                guard let self else { return }
                await self.receiveAudio(from: socket, into: buffer, initialChannels: initialChannels)
            }
        } catch {
            isConnecting = false
            disconnect(message: "连接失败：\(error.localizedDescription)")
        }
    }

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

        // Touch inputNode BEFORE attaching playback chain when recording so AVAudioEngine
        // constructs a full-duplex IO graph rather than an output-only graph.
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
            guard let self, !Task.isCancelled, self.isConnected,
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

    private func receiveAudio(from socket: URLSessionWebSocketTask, into buffer: PCMBuffer, initialChannels: Int) async {
        var currentChannels = initialChannels
        while !Task.isCancelled {
            do {
                switch try await socket.receive() {
                case .data(let data):
                    buffer.append(data, channels: currentChannels)
                case .string(let message):
                    if let data = message.data(using: .utf8),
                       let updated = try? JSONDecoder().decode(ServerConfig.self, from: data) {
                        if (1...2).contains(updated.channels) {
                            currentChannels = updated.channels
                        }
                        await MainActor.run {
                            self.deviceName = updated.deviceName ?? self.deviceName
                            if updated.sampleRate != self.sampleRate {
                                self.status = "电脑采样率已变化，请断开后重新连接。"
                            }
                        }
                    }
                @unknown default:
                    break
                }
            } catch {
                guard !Task.isCancelled else { break }
                await MainActor.run {
                    self.disconnect(message: "音频连接中断：\(error.localizedDescription)")
                }
                break
            }
        }
    }

    @MainActor
    private func sendPlaybackInfo(_ rate: Int, on socket: URLSessionWebSocketTask) {
        let payload = ["type": "audio-info", "playbackSampleRate": rate] as [String: Any]
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let text = String(data: data, encoding: .utf8) else { return }
        Task { try? await socket.send(.string(text)) }
    }

    @MainActor
    private func startMicrophone() async {
        guard isConnected, let config = serverConfig,
              config.micAvailable == true,
              let targetRate = config.micRate, targetRate > 0,
              let targetChannels = config.micChannels, (1...2).contains(targetChannels),
              let session,
              let buffer = pcmBuffer else {
            status = "电脑端未检测到 VB-Cable，无法回传麦克风。"
            return
        }

        let audioSession = AVAudioSession.sharedInstance()
        let allowed = await withCheckedContinuation { continuation in
            audioSession.requestRecordPermission { granted in continuation.resume(returning: granted) }
        }
        guard allowed else {
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
                        await MainActor.run { self.status = "麦克风回传中断：\(error.localizedDescription)" }
                        break
                    }
                }
            }

            isMicActive = true
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
    private func stopMicrophone() {
        routeRebuildTask?.cancel()
        routeRebuildTask = nil
        micContinuation?.finish()
        micContinuation = nil
        micSendTask?.cancel()
        micSendTask = nil
        micSocket?.cancel(with: .normalClosure, reason: nil)
        micSocket = nil
        isMicActive = false
        if isConnected, let config = serverConfig, let buffer = pcmBuffer {
            do {
                _ = try rebuildAudioEngine(
                    playerSampleRate: config.sampleRate,
                    buffer: buffer,
                    micTarget: nil
                )
                if let audioSocket {
                    let outRate = Int(AVAudioSession.sharedInstance().sampleRate.rounded())
                    if outRate > 0 {
                        sendPlaybackInfo(outRate, on: audioSocket)
                    }
                }
                status = "已连接 · \(deviceName) · \(sampleRate ?? config.sampleRate) Hz"
            } catch {
                status = "麦克风已停止；音频路由恢复失败：\(error.localizedDescription)"
            }
        } else {
            teardownEngineOnly()
        }
    }

    @MainActor
    private func disconnect(message: String) {
        routeRebuildTask?.cancel()
        routeRebuildTask = nil
        if isMicActive {
            micContinuation?.finish()
            micContinuation = nil
            micSendTask?.cancel()
            micSendTask = nil
            micSocket?.cancel(with: .normalClosure, reason: nil)
            micSocket = nil
            isMicActive = false
        }
        receiveTask?.cancel()
        receiveTask = nil
        audioSocket?.cancel(with: .normalClosure, reason: nil)
        audioSocket = nil
        teardownEngineOnly()
        pcmBuffer = nil
        session?.invalidateAndCancel()
        session = nil
        isConnected = false
        isConnecting = false
        deviceName = ""
        sampleRate = nil
        micAvailable = false
        status = message
    }

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
            self.pendingTrust = PendingTrust(trust: trust,
                                             certificate: certificate,
                                             fingerprint: fingerprint,
                                             host: host,
                                             pinKey: pinKey,
                                             completion: completion)
            self.certificateFingerprint = fingerprint
            self.isTrustPromptPresented = true
        }
    }

    func urlSession(_ session: URLSession,
                    didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        handleTrustChallenge(challenge, completion: completionHandler)
    }

    func urlSession(_ session: URLSession,
                    task: URLSessionTask,
                    didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
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