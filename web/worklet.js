/**
 * AudioWorklet processors for iPhone Audio Bridge.
 * Runs on the dedicated Web Audio rendering thread to avoid main-thread UI/background jitter.
 */

const RING_CAPACITY = 16384; // Power of 2 (~341 ms at 48 kHz)
const RING_MASK = RING_CAPACITY - 1;
const RESAMPLE_TAPS = 64;
const RESAMPLE_LEFT = RESAMPLE_TAPS / 2 - 1;
const RESAMPLE_PHASES = 256;

class PcmPlayerProcessor extends AudioWorkletProcessor {
    constructor(options) {
        super();
        const opts = (options && options.processorOptions) || {};

        this.inputSampleRate = opts.sampleRate || sampleRate;
        this.inputChannels = opts.channels || 2;

        this.ringL = new Float32Array(RING_CAPACITY);
        this.ringR = new Float32Array(RING_CAPACITY);
        this.readPos = 0;
        this.writePos = 0;
        this.available = 0;
        this.readFrac = 0;
        this.historyFrames = 0;

        this._updateQueueLimits();
        this._updateResampler();

        this.port.onmessage = (event) => {
            const data = event.data;

            if (data instanceof ArrayBuffer) {
                this._pushPcm(new Int16Array(data));
                return;
            }

            if (data && data.type === "config") {
                if (data.sampleRate) {
                    this.inputSampleRate = data.sampleRate;
                    this._updateQueueLimits();
                    this._updateResampler();
                }
                if (data.channels) {
                    this.inputChannels = data.channels;
                }
            } else if (data && data.type === "clear") {
                this.readPos = 0;
                this.writePos = 0;
                this.available = 0;
                this.readFrac = 0;
                this.historyFrames = 0;
            }
        };
    }

    _updateQueueLimits() {
        // Keep latency tight: cap at ~120 ms, trim back to ~60 ms on burst arrival
        this.maxFrames = Math.min(RING_CAPACITY - 256, Math.max(1024, Math.floor(this.inputSampleRate * 0.12)));
        this.keepFrames = Math.max(512, Math.floor(this.inputSampleRate * 0.06));
    }

    _updateResampler() {
        const cutoff = Math.min(1, sampleRate / this.inputSampleRate);
        this.resampleCoefficients = new Float32Array(RESAMPLE_PHASES * RESAMPLE_TAPS);

        for (let phase = 0; phase < RESAMPLE_PHASES; phase++) {
            const fraction = phase / RESAMPLE_PHASES;
            const offset = phase * RESAMPLE_TAPS;
            let total = 0;

            for (let tap = 0; tap < RESAMPLE_TAPS; tap++) {
                const distance = tap - RESAMPLE_LEFT - fraction;
                const normalizedDistance = distance / (RESAMPLE_TAPS / 2);
                const window = Math.abs(normalizedDistance) < 1
                    ? 0.42 + 0.5 * Math.cos(Math.PI * normalizedDistance)
                        + 0.08 * Math.cos(2 * Math.PI * normalizedDistance)
                    : 0;
                const scaledDistance = cutoff * distance;
                const sinc = Math.abs(scaledDistance) < 1e-8
                    ? 1
                    : Math.sin(Math.PI * scaledDistance) / (Math.PI * scaledDistance);
                const coefficient = cutoff * sinc * window;

                this.resampleCoefficients[offset + tap] = coefficient;
                total += coefficient;
            }

            for (let tap = 0; tap < RESAMPLE_TAPS; tap++) {
                this.resampleCoefficients[offset + tap] /= total;
            }
        }
    }

    _pushPcm(pcm) {
        const ch = this.inputChannels;
        if (ch < 1) return;

        const frames = Math.floor(pcm.length / ch);
        if (frames <= 0) return;

        // If a single burst exceeds ring capacity, only keep the newest tail
        let startFrame = 0;
        if (frames > this.keepFrames) {
            startFrame = frames - this.keepFrames;
            this.readPos = 0;
            this.writePos = 0;
            this.available = 0;
            this.readFrac = 0;
            this.historyFrames = 0;
        }

        const count = frames - startFrame;
        if (this.available + count > RING_CAPACITY) {
            const drop = this.available + count - RING_CAPACITY;
            this.readPos = (this.readPos + drop) & RING_MASK;
            this.available -= drop;
            this.readFrac = 0;
            this.historyFrames = Math.min(RESAMPLE_LEFT, this.historyFrames + drop);
        }

        let w = this.writePos;
        const scale = 1 / 32768;

        if (ch === 1) {
            for (let i = startFrame; i < frames; i++) {
                const s = pcm[i] * scale;
                this.ringL[w] = s;
                this.ringR[w] = s;
                w = (w + 1) & RING_MASK;
            }
        } else {
            for (let i = startFrame; i < frames; i++) {
                const idx = i * ch;
                this.ringL[w] = pcm[idx] * scale;
                this.ringR[w] = pcm[idx + 1] * scale;
                w = (w + 1) & RING_MASK;
            }
        }

        this.writePos = w;
        this.available += count;

        if (this.available > this.maxFrames) {
            const drop = this.available - this.keepFrames;
            this.readPos = (this.readPos + drop) & RING_MASK;
            this.available = this.keepFrames;
            this.readFrac = 0;
            this.historyFrames = Math.min(RESAMPLE_LEFT, this.historyFrames + drop);
        }
    }

    process(inputs, outputs) {
        const output = outputs[0];
        if (!output || output.length === 0) return true;

        const outL = output[0];
        const outR = output.length > 1 ? output[1] : null;
        const len = outL.length;

        const step = this.inputSampleRate / sampleRate;

        if (step === 1) {
            let r = this.readPos;
            let avail = this.available;

            for (let i = 0; i < len; i++) {
                if (avail > 0) {
                    outL[i] = this.ringL[r];
                    if (outR) outR[i] = this.ringR[r];
                    r = (r + 1) & RING_MASK;
                    avail--;
                } else {
                    outL[i] = 0;
                    if (outR) outR[i] = 0;
                }
            }

            const consumed = this.available - avail;
            this.readPos = r;
            this.available = avail;
            this.historyFrames = Math.min(RESAMPLE_LEFT, this.historyFrames + consumed);
            return true;
        }

        // Windowed-sinc interpolation avoids high-frequency roll-off and aliasing when rates differ.
        let r = this.readPos;
        let avail = this.available;
        let frac = this.readFrac;

        for (let i = 0; i < len; i++) {
            if (avail > 0) {
                const phase = Math.min(RESAMPLE_PHASES - 1, Math.floor(frac * RESAMPLE_PHASES));
                const coeffOffset = phase * RESAMPLE_TAPS;
                let left = 0;
                let right = 0;

                for (let tap = 0; tap < RESAMPLE_TAPS; tap++) {
                    let sourceOffset = tap - RESAMPLE_LEFT;
                    if (sourceOffset < -this.historyFrames) {
                        sourceOffset = -this.historyFrames;
                    } else if (sourceOffset >= avail) {
                        sourceOffset = avail - 1;
                    }

                    const source = (r + sourceOffset) & RING_MASK;
                    const coefficient = this.resampleCoefficients[coeffOffset + tap];
                    left += this.ringL[source] * coefficient;
                    right += this.ringR[source] * coefficient;
                }
                outL[i] = left;
                if (outR) outR[i] = right;

                frac += step;
                const advance = Math.floor(frac);
                if (advance > 0) {
                    const actual = Math.min(advance, avail);
                    r = (r + actual) & RING_MASK;
                    avail -= actual;
                    frac -= advance;
                    this.historyFrames = Math.min(RESAMPLE_LEFT, this.historyFrames + actual);
                }
            } else {
                outL[i] = 0;
                if (outR) outR[i] = 0;
                frac = 0;
            }
        }

        this.readPos = r;
        this.available = avail;
        this.readFrac = frac;
        return true;
    }
}


class PcmMicProcessor extends AudioWorkletProcessor {
    constructor(options) {
        super();
        const opts = (options && options.processorOptions) || {};

        this.targetSampleRate = opts.targetSampleRate || sampleRate;
        this.targetChannels = Math.max(1, opts.targetChannels || 2);
        this.chunkFrames = Math.max(256, Math.round(this.targetSampleRate * 0.02)); // 20 ms chunks

        this.outBuffer = new Int16Array(this.chunkFrames * this.targetChannels);
        this.outPos = 0;

        this.resamplePos = 0;
        this.prevSample = 0;
        this.hasPrev = false;
    }

    _emitSample(s) {
        const clamped = Math.max(-1, Math.min(1, s));
        const pcm = clamped < 0 ? clamped * 32768 : clamped * 32767;
        const base = this.outPos * this.targetChannels;

        for (let c = 0; c < this.targetChannels; c++) {
            this.outBuffer[base + c] = pcm;
        }

        this.outPos++;

        if (this.outPos >= this.chunkFrames) {
            const buf = this.outBuffer.buffer;
            this.port.postMessage(buf, [buf]);
            this.outBuffer = new Int16Array(this.chunkFrames * this.targetChannels);
            this.outPos = 0;
        }
    }

    process(inputs) {
        const input = inputs[0];
        if (!input || input.length === 0 || !input[0]) {
            return true;
        }

        const ch0 = input[0];
        const len = ch0.length;
        if (len === 0) return true;

        if (this.targetSampleRate === sampleRate) {
            for (let i = 0; i < len; i++) {
                this._emitSample(ch0[i]);
            }
            return true;
        }

        // Resample from AudioContext rate to VB-Cable rate
        const step = sampleRate / this.targetSampleRate;
        let pos = this.resamplePos;

        if (!this.hasPrev) {
            this.prevSample = ch0[0];
            this.hasPrev = true;
        }

        while (pos < len) {
            const idx = Math.floor(pos);
            const frac = pos - idx;
            const s0 = idx === 0 ? this.prevSample : ch0[idx - 1];
            const s1 = ch0[idx];
            this._emitSample(s0 + (s1 - s0) * frac);
            pos += step;
        }

        this.resamplePos = pos - len;
        this.prevSample = ch0[len - 1];
        return true;
    }
}

registerProcessor("pcm-player-processor", PcmPlayerProcessor);
registerProcessor("pcm-mic-processor", PcmMicProcessor);
