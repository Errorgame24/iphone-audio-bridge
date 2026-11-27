import Foundation

final class PCMBuffer {
    private let lock = NSLock()
    private var left: [Float]
    private var right: [Float]
    private var readIndex = 0
    private var writeIndex = 0
    private var frameCount = 0
    private let prebufferFrames: Int
    private var isBuffering: Bool

    init(capacity: Int = 16_384, prebufferFrames: Int = 0) {
        let safeCapacity = max(1, capacity)
        left = Array(repeating: 0, count: safeCapacity)
        right = Array(repeating: 0, count: safeCapacity)
        self.prebufferFrames = min(max(0, prebufferFrames), safeCapacity)
        self.isBuffering = self.prebufferFrames > 0
    }

    func append(_ data: Data, channels: Int) {
        guard channels > 0 else { return }
        let bytesPerFrame = channels * 2
        let frames = data.count / bytesPerFrame
        guard frames > 0 else { return }

        data.withUnsafeBytes { rawBytes in
            guard let bytes = rawBytes.baseAddress?.assumingMemoryBound(to: UInt8.self) else { return }
            lock.lock()
            defer { lock.unlock() }

            for frame in 0..<frames {
                let base = frame * bytesPerFrame
                let leftSample = sample(from: bytes, at: base)
                let rightSample = channels > 1 ? sample(from: bytes, at: base + 2) : leftSample

                if frameCount == left.count {
                    readIndex = (readIndex + 1) % left.count
                    frameCount -= 1
                }

                left[writeIndex] = leftSample
                right[writeIndex] = rightSample
                writeIndex = (writeIndex + 1) % left.count
                frameCount += 1
            }

            if isBuffering && frameCount >= prebufferFrames {
                isBuffering = false
            }
        }
    }

    func render(left outputLeft: UnsafeMutablePointer<Float>, right outputRight: UnsafeMutablePointer<Float>, frameCount requestedFrames: Int) {
        lock.lock()
        defer { lock.unlock() }

        if isBuffering {
            if frameCount >= prebufferFrames {
                isBuffering = false
            } else {
                for frame in 0..<requestedFrames {
                    outputLeft[frame] = 0
                    outputRight[frame] = 0
                }
                return
            }
        }

        for frame in 0..<requestedFrames {
            if frameCount > 0 {
                outputLeft[frame] = left[readIndex]
                outputRight[frame] = right[readIndex]
                readIndex = (readIndex + 1) % left.count
                frameCount -= 1
            } else {
                outputLeft[frame] = 0
                outputRight[frame] = 0
                if prebufferFrames > 0 {
                    isBuffering = true
                }
            }
        }
    }

    private func sample(from bytes: UnsafePointer<UInt8>, at offset: Int) -> Float {
        let rawValue = UInt16(bytes[offset]) | (UInt16(bytes[offset + 1]) << 8)
        return Float(Int16(bitPattern: rawValue)) / 32768
    }
}