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