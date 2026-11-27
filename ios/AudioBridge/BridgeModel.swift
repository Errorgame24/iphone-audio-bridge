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

final class BridgeModel: NSObject, ObservableObject, URLSessionDelegate, URLSessionTaskDelegate {
    @Published var serverAddress = UserDefaults.standard.string(forKey: "serverAddress") ?? "" {
        didSet { UserDefaults.standard.set(serverAddress, forKey: "serverAddress") }
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
    private var pcmBuffer: PCMBuffer?
    private var micConverter: AVAudioConverter?
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

            let buffer = try startPlayer(sampleRate: config.sampleRate)

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
    private func startPlayer(sampleRate: Int) throws -> PCMBuffer {
        let audioSession = AVAudioSession.sharedInstance()
        try audioSession.setCategory(.playback, mode: .default)
        try? audioSession.setPreferredSampleRate(Double(sampleRate))
        try? audioSession.setPreferredIOBufferDuration(0.01)
        try audioSession.setActive(true)

        let audioEngine = AVAudioEngine()
        let buffer = PCMBuffer(
            capacity: max(2_048, sampleRate / 6),
            prebufferFrames: max(512, sampleRate / 30)
        )
        let format = AVAudioFormat(standardFormatWithSampleRate: Double(sampleRate), channels: 2)!
        let source = AVAudioSourceNode(format: format) { _, _, frameCount, audioBufferList -> OSStatus in
            let buffers = UnsafeMutableAudioBufferListPointer(audioBufferList)
            guard buffers.count >= 2,
                  let left = buffers[0].mData?.assumingMemoryBound(to: Float.self),
                  let right = buffers[1].mData?.assumingMemoryBound(to: Float.self) else {
                return noErr
            }
            buffer.render(left: left, right: right, frameCount: Int(frameCount))
            return noErr
        }

        audioEngine.attach(source)
        audioEngine.connect(source, to: audioEngine.mainMixerNode, format: format)
        audioEngine.prepare()
        try audioEngine.start()
        pcmBuffer = buffer
        engine = audioEngine
        return buffer
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
              let session else {
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

        do {
            try audioSession.setCategory(.playAndRecord, mode: .default, options: [.allowBluetooth, .allowBluetoothA2DP])
            try audioSession.setActive(true)

            let input = engine?.inputNode
            guard let input else { throw BridgeError.microphoneUnavailable }
            let inputFormat = input.inputFormat(forBus: 0)
            guard inputFormat.sampleRate > 0,
                  let outputFormat = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                                   sampleRate: Double(targetRate),
                                                   channels: AVAudioChannelCount(targetChannels),
                                                   interleaved: true),
                  let converter = AVAudioConverter(from: inputFormat, to: outputFormat) else {
                throw BridgeError.microphoneUnavailable
            }

            let baseURL = try validatedBaseURL()
            let socket = session.webSocketTask(with: endpoint("mic", baseURL: baseURL, secureWebSocket: true))
            micSocket = socket
            socket.resume()
            try await sendMicrophoneInfo(inputRate: Int(inputFormat.sampleRate), targetRate: targetRate, on: socket)

            var continuation: AsyncStream<Data>.Continuation!
            let stream = AsyncStream<Data>(bufferingPolicy: .bufferingNewest(5)) {
                continuation = $0
            }
            micContinuation = continuation
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

            input.removeTap(onBus: 0)
            input.installTap(onBus: 0, bufferSize: 1024, format: inputFormat) { buffer, _ in
                let ratio = outputFormat.sampleRate / inputFormat.sampleRate
                let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio + 64)
                guard let converted = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: max(64, capacity)) else { return }
                var providedInput = false
                var conversionError: NSError?
                let result = converter.convert(to: converted, error: &conversionError) { _, inputStatus in
                    if providedInput {
                        inputStatus.pointee = .noDataNow
                        return nil
                    }
                    providedInput = true
                    inputStatus.pointee = .haveData
                    return buffer
                }
                guard result != .error, converted.frameLength > 0,
                      let audioData = converted.audioBufferList.pointee.mData else { return }
                let bytesPerFrame = Int(outputFormat.streamDescription.pointee.mBytesPerFrame)
                let packet = Data(bytes: audioData, count: Int(converted.frameLength) * bytesPerFrame)
                continuation.yield(packet)
            }
            micTapInstalled = true

            if let engine, !engine.isRunning { try engine.start() }
            micConverter = converter
            isMicActive = true
            status = "麦克风已回传 · \(targetRate) Hz"
        } catch {
            stopMicrophone()
            status = "无法启动麦克风：\(error.localizedDescription)"
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
        if micTapInstalled {
            engine?.inputNode.removeTap(onBus: 0)
            micTapInstalled = false
        }
        micContinuation?.finish()
        micContinuation = nil
        micSendTask?.cancel()
        micSendTask = nil
        micSocket?.cancel(with: .normalClosure, reason: nil)
        micSocket = nil
        micConverter = nil
        isMicActive = false
        if isConnected {
            do {
                try AVAudioSession.sharedInstance().setCategory(.playback, mode: .default)
                try AVAudioSession.sharedInstance().setActive(true)
                status = "已连接 · \(deviceName) · \(sampleRate ?? 0) Hz"
            } catch {
                status = "麦克风已停止；音频路由恢复失败：\(error.localizedDescription)"
            }
        }
    }

    @MainActor
    private func disconnect(message: String) {
        if isMicActive { stopMicrophone() }
        receiveTask?.cancel()
        receiveTask = nil
        audioSocket?.cancel(with: .normalClosure, reason: nil)
        audioSocket = nil
        engine?.stop()
        engine = nil
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
    case microphoneUnavailable
    case serverResponse

    var errorDescription: String? {
        switch self {
        case .invalidAddress:
            return "请输入电脑控制台显示的 HTTPS 根地址，例如 https://192.168.1.20:8443。"
        case .invalidAudioConfig:
            return "电脑服务返回了不支持的音频格式。"
        case .microphoneUnavailable:
            return "无法获取 iPhone 麦克风音频格式。"
        case .serverResponse:
            return "电脑服务未返回有效配置，请确认地址和服务状态。"
        }
    }
}