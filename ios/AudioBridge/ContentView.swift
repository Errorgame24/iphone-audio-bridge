import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var model: BridgeModel
    @FocusState private var addressFocused: Bool

    var body: some View {
        ZStack {
            Color(red: 0.055, green: 0.065, blue: 0.09).ignoresSafeArea()

            VStack(spacing: 22) {
                VStack(spacing: 10) {
                    Image(systemName: "waveform.path")
                        .font(.system(size: 38, weight: .medium))
                        .foregroundStyle(Color.cyan)
                    Text("Audio Bridge")
                        .font(.system(size: 28, weight: .bold, design: .rounded))
                        .foregroundStyle(.white)
                    Text("把 Windows 电脑音频传到 iPhone 和耳机")
                        .font(.subheadline)
                        .foregroundStyle(.white.opacity(0.65))
                }
                .padding(.top, 28)

                VStack(alignment: .leading, spacing: 10) {
                    Text("电脑服务地址")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.white.opacity(0.65))
                    TextField("https://电脑IP:8443", text: $model.serverAddress)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .focused($addressFocused)
                        .padding(14)
                        .background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 13))
                        .foregroundStyle(.white)
                        .tint(.cyan)
                }
                .padding(18)
                .background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 18))

                VStack(spacing: 12) {
                    Button {
                        addressFocused = false
                        model.toggleConnection()
                    } label: {
                        HStack(spacing: 9) {
                            if model.isConnecting { ProgressView().tint(.white) }
                            else { Image(systemName: model.isConnected ? "stop.fill" : "play.fill") }
                            Text(model.isConnecting ? "正在连接" : model.isConnected ? "断开连接" : "连接电脑")
                        }
                        .font(.headline)
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 16)
                        .background(model.isConnected ? Color.orange.opacity(0.85) : Color.blue, in: RoundedRectangle(cornerRadius: 14))
                    }
                    .disabled(model.isConnecting)

                    if model.isConnected {
                        Button {
                            model.toggleMicrophone()
                        } label: {
                            Label(model.isMicActive ? "停止麦克风回传" : "将 iPhone 麦克风回传到电脑",
                                  systemImage: model.isMicActive ? "mic.slash.fill" : "mic.fill")
                                .font(.subheadline.weight(.semibold))
                                .foregroundStyle(.white)
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 14)
                                .background(Color.white.opacity(0.1), in: RoundedRectangle(cornerRadius: 14))
                        }
                        .disabled(!model.micAvailable && !model.isMicActive)
                        .opacity(model.micAvailable || model.isMicActive ? 1 : 0.45)
                    }
                }

                VStack(alignment: .leading, spacing: 10) {
                    Label(model.status, systemImage: model.isConnected ? "checkmark.circle.fill" : "info.circle")
                        .font(.subheadline)
                        .foregroundStyle(model.isConnected ? Color.green : Color.white.opacity(0.8))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .fixedSize(horizontal: false, vertical: true)

                    if model.isConnected {
                        Divider().overlay(Color.white.opacity(0.12))
                        HStack {
                            Label(model.deviceName, systemImage: "hifispeaker")
                            Spacer(minLength: 10)
                            Text(model.sampleRate.map { "\($0) Hz" } ?? "")
                                .monospacedDigit()
                        }
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.7))
                    }
                }
                .padding(16)
                .background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 16))

                Spacer(minLength: 0)
                Text("首次连接会显示证书指纹，请只信任你自己电脑的证书。")
                    .font(.caption2)
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.white.opacity(0.42))
                    .padding(.bottom, 12)
            }
            .padding(.horizontal, 22)
            .frame(maxWidth: 520)
        }
        .preferredColorScheme(.dark)
        .alert("信任这台电脑的证书？", isPresented: $model.isTrustPromptPresented) {
            Button("信任并继续") { model.approveServerCertificate() }
            Button("取消", role: .cancel) { model.rejectServerCertificate() }
        } message: {
            Text("请确认这是你电脑的局域网地址。证书 SHA-256 指纹：\n\n\(model.certificateFingerprint)")
        }
    }
}