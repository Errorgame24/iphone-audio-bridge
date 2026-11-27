import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var model: BridgeModel
    @FocusState private var addressFocused: Bool

    private let bgBase = Color(red: 0.047, green: 0.039, blue: 0.071)
    private let cardSurface = Color(red: 0.098, green: 0.082, blue: 0.145)
    private let cardBorder = Color(red: 0.533, green: 0.267, blue: 0.600).opacity(0.32)
    private let nightcordPurple = Color(red: 0.533, green: 0.267, blue: 0.600)
    private let nightcordGlow = Color(red: 0.725, green: 0.486, blue: 0.824)
    private let nightcordPink = Color(red: 0.894, green: 0.659, blue: 0.792)

    var body: some View {
        ZStack {
            LinearGradient(
                colors: [
                    bgBase,
                    Color(red: 0.075, green: 0.055, blue: 0.118),
                    bgBase,
                ],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            .ignoresSafeArea()

            ScrollView(showsIndicators: false) {
                VStack(spacing: 16) {
                    headerSection
                    discoverySection
                    addressCard
                    connectionButtonsSection

                    if model.isConnected {
                        nowPlayingRemoteCard
                    }

                    statusCard

                    Text("控制中心 / 锁屏 / AirPods 捏合均可直接控制电脑播放与切歌 · 首次连接请核对证书指纹")
                        .font(.caption2)
                        .multilineTextAlignment(.center)
                        .foregroundStyle(.white.opacity(0.42))
                        .padding(.top, 4)
                        .padding(.bottom, 18)
                }
                .padding(.horizontal, 20)
                .padding(.top, 16)
                .frame(maxWidth: 520)
            }
            .scrollDismissesKeyboard(.interactively)
        }
        .preferredColorScheme(.dark)
        .onAppear {
            model.startDiscoveryIfNeeded()
        }
        .alert("信任这台电脑的证书？", isPresented: $model.isTrustPromptPresented) {
            Button("信任并继续") { model.approveServerCertificate() }
            Button("取消", role: .cancel) { model.rejectServerCertificate() }
        } message: {
            Text("请确认这是你电脑的局域网地址。证书 SHA-256 指纹：\n\n\(model.certificateFingerprint)")
        }
    }

    private var headerSection: some View {
        HStack(spacing: 14) {
            Image("NightcordArtwork")
                .resizable()
                .scaledToFit()
                .frame(width: 52, height: 52)
                .clipShape(RoundedRectangle(cornerRadius: 13))
                .overlay(
                    RoundedRectangle(cornerRadius: 13)
                        .stroke(cardBorder, lineWidth: 1)
                )

            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Text("25:00")
                        .font(.system(size: 13, weight: .bold, design: .monospaced))
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(nightcordPurple.opacity(0.32), in: Capsule())
                        .foregroundStyle(nightcordPink)

                    Text("Nightcord Audio Bridge")
                        .font(.system(size: 20, weight: .bold, design: .rounded))
                        .foregroundStyle(.white)
                }

                Text("低延迟电脑音源 · 后台常驻保活 · AirPods 线控联动")
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.62))
            }

            Spacer(minLength: 0)
        }
        .padding(.vertical, 4)
    }

    private var discoverySection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label("局域网可用电脑", systemImage: "dot.radiowaves.left.and.right")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(nightcordGlow)

                Spacer()

                Button {
                    model.refreshDiscovery()
                } label: {
                    HStack(spacing: 5) {
                        if model.isScanningLAN {
                            ProgressView()
                                .controlSize(.mini)
                                .tint(nightcordPink)
                        } else {
                            Image(systemName: "arrow.clockwise")
                                .font(.caption2.weight(.bold))
                        }
                        Text(model.isScanningLAN ? "扫描中…" : "重新扫描")
                            .font(.caption.weight(.semibold))
                    }
                    .foregroundStyle(nightcordPink)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                    .background(nightcordPurple.opacity(0.22), in: Capsule())
                }
                .disabled(model.isScanningLAN)
            }

            if model.discoveredServers.isEmpty {
                HStack(spacing: 8) {
                    Image(systemName: model.isScanningLAN ? "antenna.radiowaves.left.and.right" : "wifi.exclamationmark")
                        .font(.footnote)
                        .foregroundStyle(.white.opacity(0.5))
                    Text(
                        model.isScanningLAN
                            ? "正在扫描同一 Wi-Fi 下的 Windows 音频桥电脑…"
                            : "暂未扫到电脑，确认手机与电脑在同一局域网后可点「重新扫描」或直接填地址"
                    )
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.55))
                }
                .padding(.vertical, 6)
            } else {
                VStack(spacing: 8) {
                    ForEach(model.discoveredServers) { server in
                        let isCurrentTarget = model.serverAddress.caseInsensitiveCompare(server.urlString) == .orderedSame
                        Button {
                            addressFocused = false
                            model.connectToDiscovered(server)
                        } label: {
                            HStack(spacing: 12) {
                                Image(systemName: "desktopcomputer")
                                    .font(.system(size: 17, weight: .semibold))
                                    .foregroundStyle(isCurrentTarget && model.isConnected ? Color.green : nightcordGlow)
                                    .frame(width: 32, height: 32)
                                    .background(Color.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 9))

                                VStack(alignment: .leading, spacing: 2) {
                                    Text(server.displayTitle)
                                        .font(.subheadline.weight(.semibold))
                                        .foregroundStyle(.white)
                                        .lineLimit(1)
                                    Text(server.displaySubtitle)
                                        .font(.caption2)
                                        .foregroundStyle(.white.opacity(0.6))
                                        .lineLimit(1)
                                }

                                Spacer(minLength: 8)

                                Text(isCurrentTarget && model.isConnected ? "已连接" : "一键连接")
                                    .font(.caption.weight(.bold))
                                    .foregroundStyle(isCurrentTarget && model.isConnected ? Color.green : nightcordPink)
                                    .padding(.horizontal, 10)
                                    .padding(.vertical, 6)
                                    .background(
                                        (isCurrentTarget && model.isConnected ? Color.green : nightcordPurple).opacity(0.22),
                                        in: Capsule()
                                    )
                            }
                            .padding(11)
                            .background(Color.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 12))
                            .overlay(
                                RoundedRectangle(cornerRadius: 12)
                                    .stroke(
                                        isCurrentTarget ? nightcordGlow.opacity(0.55) : Color.white.opacity(0.08),
                                        lineWidth: 1
                                    )
                            )
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
        .padding(15)
        .background(cardSurface, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(cardBorder, lineWidth: 1))
    }

    private var addressCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("电脑服务地址 (HTTPS)")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.white.opacity(0.68))

            TextField("https://电脑IP:8443", text: $model.serverAddress)
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .focused($addressFocused)
                .font(.system(.subheadline, design: .monospaced))
                .padding(13)
                .background(Color.black.opacity(0.28), in: RoundedRectangle(cornerRadius: 12))
                .overlay(
                    RoundedRectangle(cornerRadius: 12)
                        .stroke(Color.white.opacity(0.1), lineWidth: 1)
                )
                .foregroundStyle(.white)
                .tint(nightcordPink)
        }
        .padding(15)
        .background(cardSurface, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(cardBorder, lineWidth: 1))
    }

    private var connectionButtonsSection: some View {
        VStack(spacing: 11) {
            Button {
                addressFocused = false
                model.toggleConnection()
            } label: {
                HStack(spacing: 9) {
                    if model.isConnecting && !model.isConnected {
                        ProgressView().tint(.white)
                    } else {
                        Image(systemName: model.isConnected ? "stop.fill" : "bolt.horizontal.fill")
                    }
                    Text(
                        model.isConnecting && !model.isConnected
                            ? "正在连接…"
                            : model.isConnected
                                ? "断开电脑音频"
                                : "连接电脑音频"
                    )
                }
                .font(.headline)
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 15)
                .background(
                    model.isConnected
                        ? LinearGradient(
                            colors: [Color(red: 0.62, green: 0.24, blue: 0.36), Color(red: 0.45, green: 0.17, blue: 0.30)],
                            startPoint: .leading,
                            endPoint: .trailing
                        )
                        : LinearGradient(
                            colors: [nightcordPurple, Color(red: 0.40, green: 0.21, blue: 0.56)],
                            startPoint: .leading,
                            endPoint: .trailing
                        ),
                    in: RoundedRectangle(cornerRadius: 14)
                )
            }
            .disabled(model.isConnecting && !model.isConnected)

            if model.isConnected {
                VStack(spacing: 10) {
                    Button {
                        model.toggleMicrophone()
                    } label: {
                        Label(
                            model.isMicActive ? "停止麦克风回传" : "将 iPhone 麦克风回传到电脑",
                            systemImage: model.isMicActive ? "mic.slash.fill" : "mic.fill"
                        )
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 13)
                        .background(
                            model.isMicActive
                                ? Color.green.opacity(0.28)
                                : Color.white.opacity(0.08),
                            in: RoundedRectangle(cornerRadius: 13)
                        )
                    }
                    .disabled(!model.micAvailable && !model.isMicActive)
                    .opacity(model.micAvailable || model.isMicActive ? 1 : 0.45)

                    if model.micAvailable || model.isMicActive {
                        Picker("麦克风音源", selection: $model.micSourceMode) {
                            ForEach(MicSourceMode.allCases) { mode in
                                Text(mode.title).tag(mode)
                            }
                        }
                        .pickerStyle(.segmented)
                    }
                }
            }
        }
    }

    private var nowPlayingRemoteCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Image(systemName: "airpodspro")
                    .foregroundStyle(nightcordPink)
                Text("系统媒体中心 · 电脑播放控制")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(nightcordGlow)
                Spacer()
                Text(model.remoteIsPlaying ? "播放中" : "已暂停")
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(model.remoteIsPlaying ? Color.green : Color.orange)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(
                        (model.remoteIsPlaying ? Color.green : Color.orange).opacity(0.16),
                        in: Capsule()
                    )
            }

            VStack(alignment: .leading, spacing: 3) {
                Text(model.nowPlayingTitle)
                    .font(.subheadline.weight(.bold))
                    .foregroundStyle(.white)
                    .lineLimit(1)
                Text(model.nowPlayingArtist)
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.65))
                    .lineLimit(1)
            }

            HStack(spacing: 14) {
                Button {
                    model.sendMediaCommand("prev")
                } label: {
                    Image(systemName: "backward.fill")
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 11)
                        .background(Color.white.opacity(0.07), in: RoundedRectangle(cornerRadius: 11))
                }

                Button {
                    model.sendMediaCommand("toggle")
                } label: {
                    Image(systemName: model.remoteIsPlaying ? "pause.fill" : "play.fill")
                        .font(.system(size: 20, weight: .bold))
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 11)
                        .background(nightcordPurple.opacity(0.55), in: RoundedRectangle(cornerRadius: 11))
                }

                Button {
                    model.sendMediaCommand("next")
                } label: {
                    Image(systemName: "forward.fill")
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 11)
                        .background(Color.white.opacity(0.07), in: RoundedRectangle(cornerRadius: 11))
                }
            }
        }
        .padding(15)
        .background(cardSurface, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(cardBorder, lineWidth: 1))
    }

    private var statusCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(
                model.status,
                systemImage: model.isReconnecting
                    ? "arrow.triangle.2.circlepath.circle.fill"
                    : model.isConnected
                        ? "checkmark.circle.fill"
                        : "info.circle"
            )
            .font(.subheadline)
            .foregroundStyle(
                model.isReconnecting
                    ? Color.orange
                    : model.isConnected
                        ? Color.green
                        : Color.white.opacity(0.82)
            )
            .frame(maxWidth: .infinity, alignment: .leading)
            .fixedSize(horizontal: false, vertical: true)

            if model.isConnected {
                Divider().overlay(Color.white.opacity(0.1))
                HStack {
                    Label(
                        model.deviceName.isEmpty ? "电脑音频输出" : model.deviceName,
                        systemImage: "hifispeaker.fill"
                    )
                    Spacer(minLength: 10)
                    Text(model.sampleRate.map { "\($0) Hz · 后台防杀已开启" } ?? "后台防杀已开启")
                        .monospacedDigit()
                }
                .font(.caption)
                .foregroundStyle(.white.opacity(0.68))
            }
        }
        .padding(15)
        .background(cardSurface, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(cardBorder, lineWidth: 1))
    }
}