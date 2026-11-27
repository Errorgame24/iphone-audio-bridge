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
    private var keepAliveDither: Bool
    private var ditherSign: Bool = false

    /// -100 dBFS alternating float dither (~1e-5, below 16-bit PCM quantization step)
    /// prevents CoreAudio / mediaremoted from treating silent passages as dead digital zeros.
    private static let silentKeepAliveAmplitude: Float = 1.0e-5

    init(capacity: Int = 16_384, prebufferFrames: Int = 0, keepAliveDither: Bool = false) {
        let safeCapacity = max(1, capacity)
        left = Array(repeating: 0, count: safeCapacity)
        right = Array(repeating: 0, count: safeCapacity)
        self.prebufferFrames = min(max(0, prebufferFrames), safeCapacity)
        self.isBuffering = self.prebufferFrames > 0
        self.keepAliveDither = keepAliveDither
    }

    func setKeepAliveDither(_ enabled: Bool) {
        lock.lock()
        keepAliveDither = enabled
        lock.unlock()
    }

    func clear() {
        lock.lock()
        readIndex = 0
        writeIndex = 0
        frameCount = 0
        isBuffering = prebufferFrames > 0
        lock.unlock()
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
                    let s = nextSilentSample()
                    outputLeft[frame] = s
                    outputRight[frame] = s
                }
                return
            }
        }

        for frame in 0..<requestedFrames {
            if frameCount > 0 {
                let l = left[readIndex]
                let r = right[readIndex]
                readIndex = (readIndex + 1) % left.count
                frameCount -= 1
                if keepAliveDither && l == 0 && r == 0 {
                    let s = nextSilentSample()
                    outputLeft[frame] = s
                    outputRight[frame] = s
                } else {
                    outputLeft[frame] = l
                    outputRight[frame] = r
                }
            } else {
                let s = nextSilentSample()
                outputLeft[frame] = s
                outputRight[frame] = s
                if prebufferFrames > 0 {
                    isBuffering = true
                }
            }
        }
    }

    @inline(__always)
    private func nextSilentSample() -> Float {
        guard keepAliveDither else { return 0 }
        ditherSign.toggle()
        return ditherSign ? Self.silentKeepAliveAmplitude : -Self.silentKeepAliveAmplitude
    }

    private func sample(from bytes: UnsafePointer<UInt8>, at offset: Int) -> Float {
        let rawValue = UInt16(bytes[offset]) | (UInt16(bytes[offset + 1]) << 8)
        return Float(Int16(bitPattern: rawValue)) / 32768
    }
}

enum PCMMicEncoder {
    static func encodeInterleavedInt16(
        left: UnsafePointer<Float>,
        right: UnsafePointer<Float>?,
        frameCount: Int,
        targetChannels: Int
    ) -> Data {
        guard frameCount > 0, (1...2).contains(targetChannels) else { return Data() }
        let sampleCount = frameCount * targetChannels
        var data = Data(count: sampleCount * MemoryLayout<Int16>.size)
        data.withUnsafeMutableBytes { rawBuffer in
            guard let out = rawBuffer.baseAddress?.assumingMemoryBound(to: Int16.self) else { return }
            if targetChannels == 1 {
                for i in 0..<frameCount {
                    let mono = right != nil ? 0.5 * (left[i] + right![i]) : left[i]
                    out[i] = floatToInt16(mono).littleEndian
                }
            } else {
                for i in 0..<frameCount {
                    let l = left[i]
                    let r = right != nil ? right![i] : l
                    out[i * 2] = floatToInt16(l).littleEndian
                    out[i * 2 + 1] = floatToInt16(r).littleEndian
                }
            }
        }
        return data
    }

    @inline(__always)
    private static func floatToInt16(_ value: Float) -> Int16 {
        if value.isNaN { return 0 }
        let clamped = min(1.0, max(-1.0, value))
        return clamped < 0
            ? Int16(clamped * 32_768.0)
            : Int16(clamped * 32_767.0)
    }
}