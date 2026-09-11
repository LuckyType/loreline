/**
 * Turn the browser's microphone into the PCM16 the server records.
 *
 * A separate script because that is what an AudioWorklet is: the browser
 * fetches it by URL and runs it on the audio thread, so it cannot be bundled
 * into the app's module graph. It lives in `static/`, which the SvelteKit
 * build copies to `frontend/build/` verbatim and the FastAPI SPA mount serves
 * from there, so `/client-mic-worklet.js` is one URL in dev and in production.
 *
 * Deliberately not a ScriptProcessorNode: that one runs on the main thread, is
 * deprecated, and drops audio whenever a render or a fetch takes too long -
 * which, over a four-hour session, is not "whenever" but "repeatedly".
 *
 * What it does is the whole of the conversion: average whatever channels
 * arrive down to one (two channels at a table are two microphones as often as
 * they are one signal twice), clamp, scale to int16, and post a filled buffer
 * across as a transferable so nothing is copied. Everything else - the rate,
 * the resampling, the framing, the timestamps - is the server's, because the
 * server is the one clock a session can be measured against.
 */

// Samples per message. At 48 kHz this is about 21 ms, which is close enough to
// the 20 ms frame the server re-blocks into that neither side is holding audio
// back, and large enough that the message rate stays under 50 a second.
const BLOCK_SAMPLES = 1024

const INT16_MAX = 0x7fff
const INT16_MIN_SCALE = 0x8000

class ClientMicProcessor extends AudioWorkletProcessor {
	constructor() {
		super()
		this.block = new Int16Array(BLOCK_SAMPLES)
		this.filled = 0
	}

	/**
	 * @param {Float32Array[][]} inputs
	 * @returns {boolean}
	 */
	process(inputs) {
		const input = inputs[0]
		if (!input || input.length === 0 || !input[0]) return true
		const channels = input.length
		const samples = input[0].length
		for (let i = 0; i < samples; i++) {
			let sum = 0
			for (let c = 0; c < channels; c++) sum += input[c][i]
			const value = Math.max(-1, Math.min(1, sum / channels))
			// Asymmetric on purpose: int16 holds one more negative step than
			// positive, and scaling both by 0x8000 clips every full-scale peak.
			this.block[this.filled++] = value < 0 ? value * INT16_MIN_SCALE : value * INT16_MAX
			if (this.filled === BLOCK_SAMPLES) {
				this.port.postMessage(this.block.buffer, [this.block.buffer])
				this.block = new Int16Array(BLOCK_SAMPLES)
				this.filled = 0
			}
		}
		return true
	}
}

registerProcessor('client-mic', ClientMicProcessor)
