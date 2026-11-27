# iPhone Audio Bridge

> **致谢与说明 / Attribution & AI Disclosure**
> - 本仓库基于原作者 **[darknezzze/iphone-audio-bridge](https://github.com/darknezzze/iphone-audio-bridge)**（MIT License）进行二次开发，感谢原作者的开源工作。
> - **本仓库中的所有二次开发代码均完全由 AI 编写**（包括 Windows 桌面悬浮面板与托盘、AirPods / iPhone 电量实时监控、AudioWorklet 抗混叠重采样优化，以及 `ios/` 原生 SwiftUI 客户端与云端 IPA 打包流程）。
>
> *Based on [darknezzze/iphone-audio-bridge](https://github.com/darknezzze/iphone-audio-bridge) under the MIT License. All secondary development and newly added code in this repository were **written entirely by AI**.*

**Stream your Windows PC's system audio to an iPhone over Wi-Fi and listen with AirPods or any Bluetooth headphones. Optionally use the iPhone microphone as a microphone on the PC.**

The iPhone client is available in Safari, with an experimental native iOS app source in `ios/`. The PC side is a small Python server (WASAPI loopback + WebSocket) with a Windows desktop floating panel and real-time AirPods / iPhone battery monitor.

Useful when your PC has no Bluetooth adapter, or when you want to use headphones that are paired with your iPhone to listen to PC audio.

## Features

- PC system audio -> iPhone -> Bluetooth headphones / AirPods
- iPhone microphone -> PC, as a virtual microphone via VB-Cable
- Windows desktop floating panel + system tray (`desktop_ui.py`) with audio sample-rate details & one-click server restart
- Real-time AirPods / Beats (BLE Continuity) & iPhone (Apple Lockdown / BLE) battery display (`battery_monitor.py`)
- Works in Safari, no iOS app needed
- Native iOS client can be built as an unsigned IPA on GitHub Actions (`Build iOS IPA`)
- Keeps playing in the background and with the screen locked (via an `<audio>` element and Media Session; behavior depends on the iOS version)
- Low-latency tuning: short queues, drops old audio instead of lagging behind
- Sample rate and channels are detected automatically (with default playback device hot-switching)
- Self-signed HTTPS certificate is generated automatically (required by iOS for microphone access)

## How it works

```
PC audio (WASAPI loopback) -> Python server -> WebSocket -> Safari / native iOS client -> AirPods
iPhone mic -> Safari / native iOS client -> WebSocket -> Python server -> VB-Cable -> any PC app
```

## Requirements

- Windows 10/11 (WASAPI loopback is Windows-only)
- Python 3.9+
- iPhone and PC on the same Wi-Fi network
- [VB-Cable](https://vb-audio.com/Cable/) (free), only for the microphone feature

## Installation

```bash
git clone https://github.com/darknezzze/iphone-audio-bridge.git
cd iphone-audio-bridge
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

1. Pair your Bluetooth headphones with the iPhone.
2. Run the server: `python server.py`
3. Allow Python through Windows Firewall for **private networks** (port `8443`) if prompted.
4. On the iPhone, open the `https://<PC-IP>:8443` address printed in the console.
5. Safari warns about the certificate (it is self-signed): tap **Show Details -> visit this website**.
6. Tap **Connect** and play something on the PC.

### Native iOS IPA (experimental)

The native client source is under `ios/`. Push the project to GitHub and run the **Build iOS IPA** workflow from the Actions tab (or push an iOS change). Download the `AudioBridge-unsigned-IPA` artifact, then sign and install it with your existing sideloading tool. The IPA is intentionally unsigned; no Apple signing credentials are stored in the repository or GitHub Actions.

In the app, enter the HTTPS address printed by `server.py`. On first connection, verify that it is your own computer on the local network before accepting its certificate fingerprint. Playback uses iOS background audio support; microphone access is optional and requires VB-Cable on the PC.

### Using the iPhone/AirPods microphone on the PC

1. Install VB-Cable and reboot.
2. Restart the server. The console should say `Microphone routed to: CABLE Input`.
3. On the iPhone, tap **Start microphone** and allow access.
4. In Discord, Zoom, a game, etc., choose **CABLE Output (VB-Audio Virtual Cable)** as the microphone.

Do not set `CABLE Input` as the default Windows playback device, or the bridge will capture your own microphone.

## Latency

| Part | Approx. |
|---|---|
| Server capture + Wi-Fi + WebSocket | 20-50 ms |
| Browser buffers | 30-100 ms |
| Bluetooth headphones (AAC/SBC) | 120-250 ms |

Total is usually **250-350 ms** with Bluetooth headphones. Fine for music and podcasts, noticeable for video and games. The Bluetooth codec is the main limit and cannot be changed from software.

Tuning options: `CHUNK` in `server.py`, and `BUFFER_SIZE`, `MAX_QUEUE`, `KEEP_QUEUE` in `web/index.html`. If you hear crackling, increase them.

## Limitations

- **AirPods quality drops while the microphone is on.** Bluetooth switches to the call profile (mono, low quality). This is an iOS/Bluetooth limitation.
- Background playback on iOS is not guaranteed and depends on the iOS version. If audio stops, keep Safari open with auto-lock disabled.
- iOS requires HTTPS for microphone access, hence the self-signed certificate.
- Windows only. Not suitable for latency-critical use (rhythm games, live monitoring).

## Troubleshooting

- **Page does not open on the iPhone:** check the firewall, that both devices are on the same network, and that you use `https://` and port `8443`.
- **Audio is pitched or slow:** restart the server and reload the page (sample rate is detected from the device).
- **Silence:** check the iPhone silent switch and that audio is actually playing on the Windows default output device.
- **Microphone button is disabled:** VB-Cable was not found. Install it, reboot, and restart the server.
- **WebSocket fails after accepting the certificate:** reload the page, or install `cert.pem` as a trusted profile on the iPhone.

## Tested on

Windows 11 / iOS 17 / AirPods (edit this line with your own setup)

## Related projects

[SoundPush](https://github.com/FlyToRakib/soundpush), [MicYou](https://github.com/OrientCOMPASS/MicYou), [pc-webrtc-audio-streamer](https://github.com/olpapchenko/pc-webrtc-audio-streamer), [AudioRelay](https://audiorelay.net/)

## License

MIT
