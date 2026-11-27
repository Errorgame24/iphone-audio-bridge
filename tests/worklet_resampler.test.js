const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const processors = new Map();
const workletContext = {
    AudioWorkletProcessor: class {
        constructor() {
            this.port = { onmessage: null, postMessage() {} };
        }
    },
    sampleRate: 44100,
    registerProcessor(name, processor) {
        processors.set(name, processor);
    }
};

const workletPath = path.join(__dirname, "..", "web", "worklet.js");
vm.runInNewContext(fs.readFileSync(workletPath, "utf8"), workletContext, {
    filename: workletPath
});

function renderSine(inputRate, frequency, inputFrames, outputFrames) {
    const Processor = processors.get("pcm-player-processor");
    const processor = new Processor({
        processorOptions: { sampleRate: inputRate, channels: 2 }
    });
    const pcm = new Int16Array(inputFrames * 2);

    for (let frame = 0; frame < inputFrames; frame++) {
        const sample = Math.round(Math.sin(2 * Math.PI * frequency * frame / inputRate) * 16383);
        pcm[frame * 2] = sample;
        pcm[frame * 2 + 1] = sample;
    }

    processor._pushPcm(pcm);
    const left = new Float32Array(outputFrames);
    const right = new Float32Array(outputFrames);
    processor.process([], [[left, right]]);
    return left;
}

function rms(samples, start, end) {
    let sum = 0;
    for (let i = start; i < end; i++) {
        sum += samples[i] * samples[i];
    }
    return Math.sqrt(sum / (end - start));
}

test("keeps passband level while converting 48 kHz to 44.1 kHz", () => {
    const output = renderSine(48000, 1000, 2800, 2500);
    const level = rms(output, 128, output.length - 128);

    assert.ok(level > 0.34 && level < 0.37, `unexpected 1 kHz RMS level: ${level}`);
});

test("suppresses frequencies above the 44.1 kHz Nyquist limit", () => {
    const output = renderSine(48000, 25000, 12000, 4000);
    const level = rms(output, 128, output.length - 128);

    assert.ok(level < 0.05, `aliased 25 kHz signal was too loud: ${level}`);
});