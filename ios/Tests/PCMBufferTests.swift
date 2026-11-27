import Foundation
import XCTest
@testable import AudioBridge

final class PCMBufferTests: XCTestCase {
    func testAppendsInterleavedStereoPcm16AsFloatFrames() {
        let buffer = PCMBuffer(capacity: 4)
        buffer.append(pcm16([16_384, -16_384, 32_767, -32_768]), channels: 2)

        let (left, right) = render(buffer, frames: 2)
        XCTAssertEqual(left[0], 0.5, accuracy: 0.0001)
        XCTAssertEqual(right[0], -0.5, accuracy: 0.0001)
        XCTAssertEqual(left[1], Float(32_767) / 32_768, accuracy: 0.0001)
        XCTAssertEqual(right[1], -1, accuracy: 0.0001)
    }

    func testKeepsNewestFramesWhenBufferOverflows() {
        let buffer = PCMBuffer(capacity: 2)
        buffer.append(pcm16([1_000, 2_000, 3_000]), channels: 1)

        let (left, right) = render(buffer, frames: 3)
        XCTAssertEqual(left[0], Float(2_000) / 32_768, accuracy: 0.0001)
        XCTAssertEqual(left[1], Float(3_000) / 32_768, accuracy: 0.0001)
        XCTAssertEqual(right, left)
        XCTAssertEqual(left[2], 0)
    }

    func testKeepAliveDitherEmitsInaudibleNonZeroSignalWhenSilent() {
        let buffer = PCMBuffer(capacity: 8, prebufferFrames: 4, keepAliveDither: true)
        let (left, right) = render(buffer, frames: 4)
        for i in 0..<4 {
            XCTAssertNotEqual(left[i], 0)
            XCTAssertEqual(abs(left[i]), 1.0e-5, accuracy: 1.0e-7)
            XCTAssertEqual(left[i], right[i])
        }

        // Also replaces pure digital zero PCM packets with -100 dBFS keepalive dither
        buffer.append(pcm16([0, 0, 0, 0, 0, 0, 0, 0]), channels: 2)
        let (zeroLeft, _) = render(buffer, frames: 4)
        XCTAssertTrue(zeroLeft.allSatisfy { $0 != 0 && abs($0) <= 2.0e-5 })

        buffer.clear()
        buffer.setKeepAliveDither(false)
        let (silentLeft, _) = render(buffer, frames: 2)
        XCTAssertEqual(silentLeft, [0, 0])
    }

    func testInboundMessageParserParsesNowPlayingAndConfig() {
        let nowPlayingJson = """
        {"type":"now-playing","playing":true,"title":"Idsmile","artist":"25-ji, Nightcord de.","hostname":"DESKTOP-2500"}
        """
        let event1 = InboundMessageParser.parse(nowPlayingJson)
        guard case let .nowPlaying(snap) = event1 else {
            return XCTFail("Expected .nowPlaying event")
        }
        XCTAssertEqual(snap.playing, true)
        XCTAssertEqual(snap.title, "Idsmile")
        XCTAssertEqual(snap.artist, "25-ji, Nightcord de.")
        XCTAssertEqual(snap.hostname, "DESKTOP-2500")

        let configJson = """
        {"type":"config","sampleRate":48000,"channels":2,"deviceName":"CABLE Input","hostname":"DESKTOP-2500"}
        """
        let event2 = InboundMessageParser.parse(configJson)
        guard case let .config(cfg) = event2 else {
            return XCTFail("Expected .config event")
        }
        XCTAssertEqual(cfg.sampleRate, 48000)
        XCTAssertEqual(cfg.channels, 2)
        XCTAssertEqual(cfg.hostname, "DESKTOP-2500")
    }

    func testServerDiscoverySelectsSameSubnetIPv4First() {
        let chosen = ServerDiscovery.selectBestIPv4(
            candidates: ["192.168.56.1", "127.0.0.1", "10.58.10.204"],
            localIPs: ["10.58.10.88"]
        )
        XCTAssertEqual(chosen, "10.58.10.204")
        XCTAssertFalse(ServerDiscovery.isValidLANIPv4("127.0.0.1"))
        XCTAssertFalse(ServerDiscovery.isValidLANIPv4("169.254.10.2"))
        XCTAssertTrue(ServerDiscovery.isValidLANIPv4("192.168.1.15"))
    }

    func testMonoMicFloatsDuplicateToInterleavedStereoInt16() {
        let samples: [Float] = [0.5, -1.5, 1.5]
        let packet = samples.withUnsafeBufferPointer { buf in
            PCMMicEncoder.encodeInterleavedInt16(
                left: buf.baseAddress!,
                right: nil,
                frameCount: samples.count,
                targetChannels: 2
            )
        }
        XCTAssertEqual(packet.count, 3 * 2 * 2)
        let decoded = packet.withUnsafeBytes { raw -> [Int16] in
            let ptr = raw.bindMemory(to: Int16.self)
            return ptr.map { Int16(littleEndian: $0) }
        }
        XCTAssertEqual(decoded, [16_383, 16_383, -32_768, -32_768, 32_767, 32_767])
    }

    private func render(_ buffer: PCMBuffer, frames: Int) -> ([Float], [Float]) {
        var left = Array(repeating: Float.zero, count: frames)
        var right = Array(repeating: Float.zero, count: frames)
        left.withUnsafeMutableBufferPointer { leftBuffer in
            right.withUnsafeMutableBufferPointer { rightBuffer in
                buffer.render(left: leftBuffer.baseAddress!, right: rightBuffer.baseAddress!, frameCount: frames)
            }
        }
        return (left, right)
    }

    private func pcm16(_ samples: [Int16]) -> Data {
        var data = Data()
        for sample in samples {
            var littleEndian = sample.littleEndian
            Swift.withUnsafeBytes(of: &littleEndian) { data.append(contentsOf: $0) }
        }
        return data
    }
}