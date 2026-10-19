// AudioWorklet: turns the mic (the context's native rate, e.g. 48 kHz) into
// 16 kHz mono 16-bit PCM and posts it in 20 ms frames. Runs off the main thread.

const TARGET_RATE = 16000;
const FRAME_SAMPLES = TARGET_RATE / 50; // 20 ms

class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / TARGET_RATE; // `sampleRate` is a worklet global
    this.position = 0;
    this.frame = new Int16Array(FRAME_SAMPLES);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;

    // Simple decimation: good enough for a test client.
    for (; this.position < channel.length; this.position += this.step) {
      const sample = Math.max(-1, Math.min(1, channel[Math.floor(this.position)]));
      this.frame[this.filled++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      if (this.filled === FRAME_SAMPLES) {
        this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
        this.frame = new Int16Array(FRAME_SAMPLES);
        this.filled = 0;
      }
    }
    this.position -= channel.length;
    return true;
  }
}

registerProcessor("pcm-processor", PCMProcessor);
