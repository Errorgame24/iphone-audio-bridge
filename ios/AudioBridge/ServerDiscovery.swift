import Combine
import Darwin
import Foundation
import Network

struct DiscoveredServer: Identifiable, Equatable, Hashable, Sendable {
    let host: String
    let port: Int
    var hostname: String
    var deviceName: String

    var id: String { "\(host):\(port)" }
    var urlString: String { "https://\(host):\(port)" }

    var displayTitle: String {
        let trimmed = hostname.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? host : trimmed
    }

    var displaySubtitle: String {
        let dev = deviceName.trimmingCharacters(in: .whitespacesAndNewlines)
        return dev.isEmpty ? "\(host):\(port)" : "\(host):\(port) · \(dev)"
    }
}

private struct UdpDiscoveryReply: Decodable {
    let magic: String?
    let hostname: String?
    let ip: String?
    let ips: [String]?
    let port: Int?
    let deviceName: String?
}

private struct ProbeServerConfig: Decodable {
    let sampleRate: Int
    let channels: Int
    let deviceName: String?
    let hostname: String?
    let port: Int?
}

private final class DiscoveryProbeSessionDelegate: NSObject, URLSessionDelegate {
    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        if challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
           let trust = challenge.protectionSpace.serverTrust {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            completionHandler(.performDefaultHandling, nil)
        }
    }
}

struct LocalIPv4Interface: Sendable, Equatable {
    let name: String
    let ip: String
    let prefix24: String
    let broadcast: String
}

@MainActor
final class ServerDiscovery: ObservableObject {
    @Published private(set) var servers: [DiscoveredServer] = []
    @Published private(set) var isScanning = false

    private var browser: NWBrowser?
    private var bonjourServers: [String: DiscoveredServer] = [:]
    private var probedServers: [String: DiscoveredServer] = [:]
    private var scanTask: Task<Void, Never>?
    private var endpointResolvers: [NWEndpoint: NWConnection] = [:]

    func start() {
        startBonjourBrowserIfNeeded()
        scanNow()
    }

    func stop() {
        scanTask?.cancel()
        scanTask = nil
        isScanning = false
        for (_, conn) in endpointResolvers {
            conn.cancel()
        }
        endpointResolvers.removeAll()
        browser?.cancel()
        browser = nil
    }

    func scanNow() {
        startBonjourBrowserIfNeeded()
        scanTask?.cancel()
        isScanning = true

        scanTask = Task { [weak self] in
            guard let self else { return }
            let interfaces = Self.activeLANInterfaces()
            let localIPs = interfaces.map(\.ip)

            // Phase 1: Fast UDP broadcast + /24 unicast discovery on port 8443 (~280ms)
            let udpFound = await Task.detached(priority: .utility) {
                Self.performUDPScan(interfaces: interfaces, localIPs: localIPs, port: 8443)
            }.value

            guard !Task.isCancelled else { return }
            for item in udpFound {
                self.probedServers[item.id] = item
            }
            self.rebuildMergedList()

            // Phase 2: If neither Bonjour nor UDP found a server (e.g. strict UDP firewall),
            // run bounded HTTPS /config probe across the phone's /24 subnet.
            if self.servers.isEmpty && !interfaces.isEmpty {
                let httpsFound = await Task.detached(priority: .utility) {
                    await Self.performHTTPSConfigSweep(interfaces: interfaces, port: 8443)
                }.value
                guard !Task.isCancelled else { return }
                for item in httpsFound {
                    self.probedServers[item.id] = item
                }
                self.rebuildMergedList()
            }

            self.isScanning = false
        }
    }

    private func startBonjourBrowserIfNeeded() {
        guard browser == nil else { return }
        let params = NWParameters.tcp
        params.includePeerToPeer = true
        let descriptor = NWBrowser.Descriptor.bonjourWithTXTRecord(
            type: "_audiobridge._tcp",
            domain: nil
        )
        let nwBrowser = NWBrowser(for: descriptor, using: params)
        nwBrowser.browseResultsChangedHandler = { [weak self] results, _ in
            Task { @MainActor [weak self] in
                self?.handleBonjourResults(results)
            }
        }
        nwBrowser.stateUpdateHandler = { [weak self] state in
            if case .failed = state {
                Task { @MainActor [weak self] in
                    self?.browser?.cancel()
                    self?.browser = nil
                }
            }
        }
        nwBrowser.start(queue: .global(qos: .utility))
        browser = nwBrowser
    }

    private func handleBonjourResults(_ results: Set<NWBrowser.Result>) {
        let localIPs = Self.activeLANInterfaces().map(\.ip)
        var nextBonjour: [String: DiscoveredServer] = [:]

        for result in results {
            var serviceName = "Windows 电脑"
            if case let .service(name, _, _, _) = result.endpoint {
                serviceName = name
            }

            var hostname = serviceName
            var port = 8443
            var candidateIPs: [String] = []

            if case let .bonjour(txt) = result.metadata {
                if let h = txt["hostname"], !h.isEmpty {
                    hostname = h
                }
                if let pStr = txt["port"], let p = Int(pStr), (1...65535).contains(p) {
                    port = p
                }
                if let primary = txt["ip"], !primary.isEmpty {
                    candidateIPs.append(primary)
                }
                if let multi = txt["ips"], !multi.isEmpty {
                    for part in multi.split(separator: ",") {
                        let ip = part.trimmingCharacters(in: .whitespacesAndNewlines)
                        if !ip.isEmpty && !candidateIPs.contains(ip) {
                            candidateIPs.append(ip)
                        }
                    }
                }
            }

            if let bestIP = Self.selectBestIPv4(candidates: candidateIPs, localIPs: localIPs) {
                let server = DiscoveredServer(
                    host: bestIP,
                    port: port,
                    hostname: hostname,
                    deviceName: ""
                )
                nextBonjour[server.id] = server
            } else {
                resolveBonjourEndpoint(result.endpoint, fallbackHostname: hostname, fallbackPort: port)
            }
        }

        bonjourServers = nextBonjour
        rebuildMergedList()
    }

    private func resolveBonjourEndpoint(
        _ endpoint: NWEndpoint,
        fallbackHostname: String,
        fallbackPort: Int
    ) {
        guard endpointResolvers[endpoint] == nil else { return }
        let conn = NWConnection(to: endpoint, using: .tcp)
        endpointResolvers[endpoint] = conn

        conn.stateUpdateHandler = { [weak self] state in
            switch state {
            case .ready:
                let remote = conn.currentPath?.remoteEndpoint
                conn.cancel()
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    self.endpointResolvers.removeValue(forKey: endpoint)
                    if case let .hostPort(host, nwPort) = remote,
                       case let .ipv4(v4) = host {
                        let raw = v4.rawValue
                        if raw.count == 4 {
                            let ip = "\(raw[0]).\(raw[1]).\(raw[2]).\(raw[3])"
                            let resolvedPort = Int(nwPort.rawValue) > 0 ? Int(nwPort.rawValue) : fallbackPort
                            let item = DiscoveredServer(
                                host: ip,
                                port: resolvedPort,
                                hostname: fallbackHostname,
                                deviceName: ""
                            )
                            self.bonjourServers[item.id] = item
                            self.rebuildMergedList()
                        }
                    }
                }
            case .failed, .cancelled:
                Task { @MainActor [weak self] in
                    self?.endpointResolvers.removeValue(forKey: endpoint)
                }
            default:
                break
            }
        }
        conn.start(queue: .global(qos: .utility))
    }

    private func rebuildMergedList() {
        var merged = bonjourServers
        for (key, value) in probedServers {
            if var existing = merged[key] {
                if !value.hostname.isEmpty {
                    existing.hostname = value.hostname
                }
                if !value.deviceName.isEmpty {
                    existing.deviceName = value.deviceName
                }
                merged[key] = existing
            } else {
                merged[key] = value
            }
        }
        servers = merged.values.sorted {
            if $0.displayTitle.localizedCaseInsensitiveCompare($1.displayTitle) == .orderedSame {
                return $0.host < $1.host
            }
            return $0.displayTitle.localizedCaseInsensitiveCompare($1.displayTitle) == .orderedAscending
        }
    }

    nonisolated static func selectBestIPv4(candidates: [String], localIPs: [String]) -> String? {
        let valid = candidates.filter { isValidLANIPv4($0) }
        guard !valid.isEmpty else { return nil }

        for local in localIPs {
            let parts = local.split(separator: ".")
            guard parts.count == 4 else { continue }
            let prefix24 = "\(parts[0]).\(parts[1]).\(parts[2])."
            if let match24 = valid.first(where: { $0.hasPrefix(prefix24) }) {
                return match24
            }
        }

        for local in localIPs {
            let parts = local.split(separator: ".")
            guard parts.count == 4 else { continue }
            let prefix16 = "\(parts[0]).\(parts[1])."
            if let match16 = valid.first(where: { $0.hasPrefix(prefix16) }) {
                return match16
            }
        }

        return valid.first
    }

    nonisolated static func isValidLANIPv4(_ ip: String) -> Bool {
        let parts = ip.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return false }
        var octets: [UInt8] = []
        octets.reserveCapacity(4)
        for p in parts {
            guard let v = UInt8(p) else { return false }
            octets.append(v)
        }
        if octets[0] == 127 || octets[0] == 0 || octets[0] >= 224 {
            return false
        }
        if octets[0] == 169 && octets[1] == 254 {
            return false
        }
        return true
    }

    nonisolated static func activeLANInterfaces() -> [LocalIPv4Interface] {
        var ptr: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&ptr) == 0, let first = ptr else { return [] }
        defer { freeifaddrs(first) }

        var preferred: [LocalIPv4Interface] = []
        var secondary: [LocalIPv4Interface] = []
        var cursor: UnsafeMutablePointer<ifaddrs>? = first

        while let entry = cursor {
            defer { cursor = entry.pointee.ifa_next }
            let flags = Int32(entry.pointee.ifa_flags)
            guard (flags & IFF_UP) != 0, (flags & IFF_LOOPBACK) == 0 else { continue }
            guard let sa = entry.pointee.ifa_addr, Int32(sa.pointee.sa_family) == AF_INET else { continue }

            let name = String(cString: entry.pointee.ifa_name)
            if name.hasPrefix("pdp_ip") || name.hasPrefix("ipsec") || name.hasPrefix("llw") || name.hasPrefix("awdl") {
                continue
            }

            var addrIn = sa.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { $0.pointee }
            var ipBuf = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
            guard inet_ntop(AF_INET, &addrIn.sin_addr, &ipBuf, socklen_t(INET_ADDRSTRLEN)) != nil else { continue }
            let ip = String(cString: ipBuf)
            guard isValidLANIPv4(ip) else { continue }

            let octets = ip.split(separator: ".")
            guard octets.count == 4 else { continue }
            let prefix24 = "\(octets[0]).\(octets[1]).\(octets[2])"

            var broadcast = "\(prefix24).255"
            if let maskSa = entry.pointee.ifa_netmask, Int32(maskSa.pointee.sa_family) == AF_INET {
                let maskIn = maskSa.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { $0.pointee }
                let ipHostOrder = UInt32(bigEndian: addrIn.sin_addr.s_addr)
                let maskHostOrder = UInt32(bigEndian: maskIn.sin_addr.s_addr)
                if maskHostOrder != 0 && maskHostOrder != 0xFFFF_FFFF {
                    let bcastNetOrder = ((ipHostOrder & maskHostOrder) | ~maskHostOrder).bigEndian
                    var bcastAddr = in_addr(s_addr: bcastNetOrder)
                    var bcastBuf = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
                    if inet_ntop(AF_INET, &bcastAddr, &bcastBuf, socklen_t(INET_ADDRSTRLEN)) != nil {
                        broadcast = String(cString: bcastBuf)
                    }
                }
            }

            let iface = LocalIPv4Interface(name: name, ip: ip, prefix24: prefix24, broadcast: broadcast)
            if name.hasPrefix("en") || name.hasPrefix("bridge") {
                if !preferred.contains(where: { $0.ip == ip }) {
                    preferred.append(iface)
                }
            } else if !secondary.contains(where: { $0.ip == ip }) {
                secondary.append(iface)
            }
        }

        return preferred.isEmpty ? secondary : preferred
    }

    nonisolated private static func performUDPScan(
        interfaces: [LocalIPv4Interface],
        localIPs: [String],
        port: Int
    ) -> [DiscoveredServer] {
        let fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
        guard fd >= 0 else { return [] }
        defer { close(fd) }

        var yes: Int32 = 1
        _ = setsockopt(fd, SOL_SOCKET, SO_BROADCAST, &yes, socklen_t(MemoryLayout<Int32>.size))
        var timeout = timeval(tv_sec: 0, tv_usec: 90_000)
        _ = setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))

        let probeBytes = Array("AUDIOBRIDGE_DISCOVER_V1".utf8)

        func sendProbe(to host: String) {
            var target = sockaddr_in()
            target.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
            target.sin_family = sa_family_t(AF_INET)
            target.sin_port = UInt16(port).bigEndian
            guard inet_pton(AF_INET, host, &target.sin_addr) == 1 else { return }
            probeBytes.withUnsafeBytes { raw in
                guard let base = raw.baseAddress else { return }
                withUnsafePointer(to: &target) { ptr in
                    ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                        _ = sendto(fd, base, probeBytes.count, 0, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
                    }
                }
            }
        }

        sendProbe(to: "255.255.255.255")
        for iface in interfaces.prefix(2) {
            sendProbe(to: iface.broadcast)
            for hostPart in 1...254 {
                let candidate = "\(iface.prefix24).\(hostPart)"
                if candidate != iface.ip {
                    sendProbe(to: candidate)
                }
            }
        }

        var discovered: [String: DiscoveredServer] = [:]
        let deadline = Date().addingTimeInterval(0.32)
        var recvBuf = [UInt8](repeating: 0, count: 2048)

        while Date() < deadline {
            var sender = sockaddr_in()
            var senderLen = socklen_t(MemoryLayout<sockaddr_in>.size)
            let count = recvBuf.withUnsafeMutableBytes { raw -> Int in
                guard let base = raw.baseAddress else { return -1 }
                return withUnsafeMutablePointer(to: &sender) { ptr in
                    ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                        recvfrom(fd, base, raw.count, 0, sa, &senderLen)
                    }
                }
            }

            if count <= 0 {
                continue
            }

            var senderIPBuf = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
            var sinAddr = sender.sin_addr
            let senderIP: String? = inet_ntop(
                AF_INET,
                &sinAddr,
                &senderIPBuf,
                socklen_t(INET_ADDRSTRLEN)
            ) != nil ? String(cString: senderIPBuf) : nil

            let data = Data(recvBuf.prefix(count))
            guard let reply = try? JSONDecoder().decode(UdpDiscoveryReply.self, from: data),
                  reply.magic == "AUDIOBRIDGE_HERE_V1" else {
                continue
            }

            var ips: [String] = []
            if let sIP = senderIP, isValidLANIPv4(sIP) {
                ips.append(sIP)
            }
            if let pIP = reply.ip, !ips.contains(pIP) {
                ips.append(pIP)
            }
            for extra in reply.ips ?? [] where !ips.contains(extra) {
                ips.append(extra)
            }

            guard let chosenHost = selectBestIPv4(candidates: ips, localIPs: localIPs) else {
                continue
            }
            let serverPort = (reply.port ?? port)
            let item = DiscoveredServer(
                host: chosenHost,
                port: serverPort,
                hostname: reply.hostname ?? chosenHost,
                deviceName: reply.deviceName ?? ""
            )
            discovered[item.id] = item
        }

        return Array(discovered.values)
    }

    nonisolated private static func performHTTPSConfigSweep(
        interfaces: [LocalIPv4Interface],
        port: Int
    ) async -> [DiscoveredServer] {
        guard let iface = interfaces.first else { return [] }
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 0.65
        config.timeoutIntervalForResource = 0.75
        config.waitsForConnectivity = false
        let delegate = DiscoveryProbeSessionDelegate()
        let probeSession = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        defer { probeSession.invalidateAndCancel() }

        let candidates = (1...254)
            .map { "\(iface.prefix24).\($0)" }
            .filter { $0 != iface.ip }

        var results: [DiscoveredServer] = []
        let maxConcurrency = 28

        await withTaskGroup(of: DiscoveredServer?.self) { group in
            var index = 0
            while index < min(maxConcurrency, candidates.count) {
                let ip = candidates[index]
                index += 1
                group.addTask {
                    await probeSingleHost(ip: ip, port: port, session: probeSession)
                }
            }

            for await found in group {
                if let found {
                    results.append(found)
                }
                if !Task.isCancelled && index < candidates.count {
                    let ip = candidates[index]
                    index += 1
                    group.addTask {
                        await probeSingleHost(ip: ip, port: port, session: probeSession)
                    }
                }
            }
        }

        return results
    }

    nonisolated private static func probeSingleHost(
        ip: String,
        port: Int,
        session: URLSession
    ) async -> DiscoveredServer? {
        guard let url = URL(string: "https://\(ip):\(port)/config") else { return nil }
        var req = URLRequest(url: url)
        req.timeoutInterval = 0.65
        do {
            let (data, response) = try await session.data(for: req)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
            let cfg = try JSONDecoder().decode(ProbeServerConfig.self, from: data)
            guard cfg.sampleRate > 0, (1...2).contains(cfg.channels) else { return nil }
            return DiscoveredServer(
                host: ip,
                port: cfg.port ?? port,
                hostname: cfg.hostname ?? ip,
                deviceName: cfg.deviceName ?? ""
            )
        } catch {
            return nil
        }
    }
}
