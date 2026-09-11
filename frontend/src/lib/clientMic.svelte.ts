/**
 * This device's microphone, as a source the server can record.
 *
 * The machine running Loreline may have no sound hardware at all; the laptop
 * on the table always does. This store is that half of the deal (ADR 0010):
 * `getUserMedia`, an AudioWorklet that turns Float32 into PCM16, and a socket
 * carrying the frames to `WS /ws/audio/capture`, where they become an ordinary
 * `CaptureSource` and nothing downstream can tell the difference.
 *
 * One instance per browser session, module-level, because the microphone is:
 * navigating from the Dashboard to a session page must not drop the audio, and
 * two tabs cannot both be the source (the server refuses the second socket).
 *
 * Four things can be wrong here and they have four different fixes, which is
 * why `notice` distinguishes them rather than collapsing to "microphone
 * unavailable": no secure context (nothing in the page can fix it), permission
 * denied (the address bar), no input device (the hardware), and a socket that
 * is not connected (the server or the network). A dead picker and a generic
 * permission error are the two ways this feature fails silently.
 */

import { backoffDelay } from './ws'

/** What the microphone itself is doing. The socket is tracked separately: a
 *  granted microphone with a dead socket is a real and separately fixable
 *  state, and so is the reverse. */
export type MicState =
	| 'insecure' // window.isSecureContext === false; no site gets a microphone here
	| 'unsupported' // no mediaDevices at all (an ancient or locked-down browser)
	| 'idle' // not asked for yet, or released
	| 'starting' // the permission prompt is up, or the graph is being built
	| 'streaming' // frames are going to the server
	| 'denied' // the browser refused, or the user did
	| 'no-device' // nothing to record with
	| 'error' // anything else, with the browser's own words in `error`

/** Whether the frames are reaching the server right now. */
export type SocketState = 'closed' | 'connecting' | 'open'

/** One input the browser is willing to record from. */
export interface MicDevice {
	id: string
	label: string
}

/** What the capture card shows about the client microphone, if anything. */
export interface MicNotice {
	tone: 'error' | 'warn' | 'info'
	text: string
}

/**
 * The constraints, and why every one of them is off.
 *
 * `getUserMedia` defaults echo cancellation, noise suppression and automatic
 * gain control to on. That default is tuned for one person on a headset with a
 * loudspeaker to cancel, and every part of it is wrong for a microphone in the
 * middle of a table with five people around it:
 *
 * - **Echo cancellation** wants a reference signal from this device's own
 *   output. With nothing playing it gates and ducks for no gain; with
 *   something playing (a soundtrack, a virtual tabletop) it removes exactly
 *   the room audio that also reached the microphone.
 * - **Noise suppression** is trained on one near voice against stationary
 *   noise. The quieter, further speakers at a table read as noise, and it
 *   gates them out - which is the opposite of what a diarizer needs.
 * - **Automatic gain control** raises the floor during pauses, bringing up
 *   room noise and the VAD's false positives with it, and ducks whoever is
 *   loudest. Diarization clusters on relative level, so this actively
 *   destroys the signal it depends on.
 *
 * All three are lossy, applied before any code here sees a sample, and cannot
 * be undone by re-processing the stored WAV later. The server's pipeline
 * (Silero, the vendor's model, the diarizer) is built for the raw room audio
 * `SoundDeviceSource` delivers, so this has to deliver the same thing or "the
 * same session either way" stops being true. Stated in the UI as well as here,
 * because a GM wearing a headset would reasonably want the opposite.
 */
const ROOM_MIC_CONSTRAINTS = {
	echoCancellation: false,
	noiseSuppression: false,
	autoGainControl: false,
	channelCount: 1,
} as const

/** Bytes allowed to pile up unsent before frames are dropped. A stalled
 *  uplink otherwise grows this without bound and the tab dies of memory
 *  rather than of a dropped connection, which is the recoverable failure. */
const MAX_BUFFERED_BYTES = 512 * 1024
/** How often the meter is repainted. Frames arrive 50 times a second; an eye
 *  reading a bar graph does not need 50 updates. */
const LEVEL_INTERVAL_MS = 100
/** Full scale for int16, for the 0-1 peak the shared LevelMeter draws. */
const INT16_FULL_SCALE = 32768

/** A wake lock, as much of it as this needs. Typed here rather than relying on
 *  the DOM lib, which does not carry it in every TypeScript version. */
interface WakeLock {
	released: boolean
	release: () => Promise<void>
	addEventListener: (type: 'release', listener: () => void) => void
}

class ClientMicStore {
	/** What the microphone is doing. See `notice` for what to say about it. */
	state = $state<MicState>('idle')
	/** Whether the frames are reaching the server. */
	socket = $state<SocketState>('closed')
	/** The browser's own words for whatever went wrong, or ''. */
	error = $state('')
	/** Why the server closed the socket, when it said (another tab is
	 *  streaming, the login expired). Cleared as soon as one connects. */
	socketReason = $state('')
	/** The inputs this browser will offer. Labels are blank until permission
	 *  has been granted once, which is why "Use this microphone" comes first
	 *  and the list fills in afterwards. */
	devices = $state.raw<MicDevice[]>([])
	/** The chosen input's id, or '' for whatever the browser considers default. */
	deviceId = $state('')
	/** 0-1 peak of the audio going out, for the shared LevelMeter. */
	peak = $state(0)
	/** Whether the screen wake lock was actually granted. A browser can refuse
	 *  it (battery saver, an unfocused tab), and the card says so rather than
	 *  promising a lid that stays awake. */
	wakeLockHeld = $state(false)

	#stream: MediaStream | null = null
	#context: AudioContext | null = null
	#node: AudioWorkletNode | null = null
	#silence: GainNode | null = null
	#ws: WebSocket | null = null
	#retry: ReturnType<typeof setTimeout> | null = null
	#attempt = 0
	#closing = false
	#wakeLock: WakeLock | null = null
	#hold = 0
	#lastLevelAt = 0

	/** True while this tab is the microphone, which is the only way any page
	 *  can tell "this browser is the source" from "some browser is". */
	get streaming(): boolean {
		return this.state === 'streaming'
	}

	/** Whether this page could record at all, before anything is asked of it.
	 *  False on plain HTTP, where no application code can change the answer. */
	get available(): boolean {
		return this.state !== 'insecure' && this.state !== 'unsupported'
	}

	/** True once the server is receiving audio from this tab: the one
	 *  condition a client-source session can be started on. */
	get ready(): boolean {
		return this.streaming && this.socket === 'open'
	}

	/**
	 * The one thing to show about the client microphone right now.
	 *
	 * Four distinct failures with four distinct fixes, each said as a whole
	 * sentence that names where the fix is. Collapsing them was the failure
	 * mode this exists to avoid: "could not access the microphone" sends
	 * somebody hunting through browser settings for a permission that was
	 * never the problem.
	 */
	get notice(): MicNotice | null {
		if (this.state === 'insecure') {
			return {
				tone: 'error',
				text:
					'This page is not a secure context, so no browser will give it a microphone. ' +
					'Open Loreline over HTTPS (see "Recording from a laptop" in the README) or over ' +
					'http://localhost on the machine itself, and this device can be the microphone. ' +
					'Nothing on this page can change it.',
			}
		}
		if (this.state === 'unsupported') {
			return {
				tone: 'error',
				text:
					'This browser does not offer microphone capture at all. Use a current Firefox, ' +
					'Chrome or Safari, or record from the server’s own microphone instead.',
			}
		}
		if (this.state === 'denied') {
			return {
				tone: 'error',
				text:
					'This browser refused the microphone. Allow it for this site in the address bar’s ' +
					'permissions, reload the page, and try again.',
			}
		}
		if (this.state === 'no-device') {
			return {
				tone: 'error',
				text:
					'This device has no microphone the browser can see. Plug one in and press "Use this ' +
					'microphone" again, or record from the server’s own microphone instead.',
			}
		}
		if (this.state === 'error') {
			return {
				tone: 'error',
				text: `The microphone could not be opened: ${this.error}`,
			}
		}
		if (this.streaming && this.socket !== 'open') {
			return {
				tone: 'error',
				text:
					'The microphone is on, but its audio is not reaching Loreline: the connection to the ' +
					'server is down. ' +
					(this.socketReason ? `The server said: ${this.socketReason}. ` : '') +
					'It keeps retrying; if it does not come back, reload the page.',
			}
		}
		return null
	}

	/** Ask for the microphone, build the graph and start streaming.
	 *
	 * Deliberately only ever called from a click. Asking on page load would
	 * light the browser's recording indicator for anyone who opened the
	 * dashboard, and it is the grant that fills in the device labels, so the
	 * first interaction has to be this one either way. */
	async start(deviceId: string = this.deviceId): Promise<void> {
		if (typeof window === 'undefined') return
		if (!window.isSecureContext) {
			this.state = 'insecure'
			return
		}
		if (!navigator.mediaDevices?.getUserMedia) {
			this.state = 'unsupported'
			return
		}
		this.stop()
		this.state = 'starting'
		this.error = ''
		try {
			this.#stream = await navigator.mediaDevices.getUserMedia({
				audio: deviceId
					? { deviceId: { exact: deviceId }, ...ROOM_MIC_CONSTRAINTS }
					: { ...ROOM_MIC_CONSTRAINTS },
			})
		} catch (err) {
			this.#failed(err)
			return
		}
		this.deviceId = deviceId
		await this.refreshDevices()
		try {
			await this.#buildGraph()
		} catch (err) {
			this.stop()
			this.state = 'error'
			this.error = err instanceof Error ? err.message : String(err)
			return
		}
		this.#openSocket()
		void this.#takeWakeLock()
		this.state = 'streaming'
	}

	/** Release everything, so the browser's recording indicator goes out. */
	stop(): void {
		this.#closing = true
		if (this.#retry) clearTimeout(this.#retry)
		this.#retry = null
		this.#ws?.close()
		this.#ws = null
		this.socket = 'closed'
		this.#node?.disconnect()
		this.#node = null
		this.#silence?.disconnect()
		this.#silence = null
		void this.#context?.close().catch(() => {})
		this.#context = null
		for (const track of this.#stream?.getTracks() ?? []) track.stop()
		this.#stream = null
		void this.#releaseWakeLock()
		this.peak = 0
		this.#hold = 0
		if (this.available) this.state = 'idle'
	}

	/** The inputs this browser lists, with whatever labels it will give.
	 *
	 * Labels are blank until a grant, which is a privacy rule and not an
	 * error: a picker full of "Microphone (2c9a…)" is what this looks like
	 * before "Use this microphone" has ever been pressed. */
	async refreshDevices(): Promise<void> {
		if (typeof window === 'undefined' || !navigator.mediaDevices?.enumerateDevices) return
		if (!window.isSecureContext) {
			this.state = 'insecure'
			return
		}
		try {
			const all = await navigator.mediaDevices.enumerateDevices()
			this.devices = all
				.filter((device) => device.kind === 'audioinput')
				.map((device, index) => ({
					id: device.deviceId,
					label: device.label || `Microphone ${index + 1}`,
				}))
		} catch {
			// Enumeration failing says nothing a GM can act on, and the picker
			// is allowed to be empty: "Use this microphone" still works and
			// fills it in.
		}
	}

	/** Check, without asking for anything, whether this page can record.
	 *  Called when the capture card mounts, so the insecure-context sentence
	 *  is on screen before anyone presses a button that cannot work. */
	probe(): void {
		if (typeof window === 'undefined') return
		if (!window.isSecureContext) {
			this.state = 'insecure'
			return
		}
		if (!navigator.mediaDevices?.getUserMedia) {
			this.state = 'unsupported'
			return
		}
		if (!this.available) this.state = 'idle'
		void this.refreshDevices()
	}

	#failed(err: unknown): void {
		const name = err instanceof Error ? err.name : ''
		this.error = err instanceof Error ? err.message : String(err)
		if (name === 'NotAllowedError' || name === 'SecurityError') this.state = 'denied'
		else if (name === 'NotFoundError' || name === 'OverconstrainedError') this.state = 'no-device'
		else this.state = 'error'
		for (const track of this.#stream?.getTracks() ?? []) track.stop()
		this.#stream = null
	}

	async #buildGraph(): Promise<void> {
		const stream = this.#stream
		if (!stream) throw new Error('the microphone stream went away')
		// The context's own rate, not a forced one: asking for 16 kHz here is
		// refused outright by some browsers and resampled by an unknown filter
		// in the rest. The server has one resampler for every source and knows
		// which rate the provider wants, so it does this once, well.
		const context = new AudioContext()
		this.#context = context
		await context.audioWorklet.addModule('/client-mic-worklet.js')
		await context.resume()
		const node = new AudioWorkletNode(context, 'client-mic')
		node.port.onmessage = (event: MessageEvent<ArrayBuffer>) => this.#send(event.data)
		// An AudioWorkletNode only runs while it is part of a graph that
		// reaches the destination, so the audio is routed there through a gain
		// of zero: connected enough to be pulled, silent enough not to play
		// the table back at itself.
		const silence = context.createGain()
		silence.gain.value = 0
		context.createMediaStreamSource(stream).connect(node)
		node.connect(silence)
		silence.connect(context.destination)
		this.#node = node
		this.#silence = silence
	}

	#send(buffer: ArrayBuffer): void {
		this.#meter(buffer)
		const ws = this.#ws
		if (!ws || ws.readyState !== WebSocket.OPEN) return
		// A stalled uplink is a dropped connection that has not noticed yet.
		// Dropping the newest frames keeps the tab alive to reconnect, which is
		// the only outcome that recovers anything.
		if (ws.bufferedAmount > MAX_BUFFERED_BYTES) return
		ws.send(buffer)
	}

	#meter(buffer: ArrayBuffer): void {
		const samples = new Int16Array(buffer)
		let peak = 0
		for (let i = 0; i < samples.length; i++) {
			const value = Math.abs(samples[i])
			if (value > peak) peak = value
		}
		this.#hold = Math.max(this.#hold, peak / INT16_FULL_SCALE)
		const now = Date.now()
		if (now - this.#lastLevelAt < LEVEL_INTERVAL_MS) return
		this.#lastLevelAt = now
		this.peak = this.#hold
		this.#hold = 0
	}

	#openSocket(): void {
		this.#closing = false
		this.socket = 'connecting'
		const proto = location.protocol === 'https:' ? 'wss' : 'ws'
		const ws = new WebSocket(`${proto}://${location.host}/ws/audio/capture`)
		ws.binaryType = 'arraybuffer'
		this.#ws = ws
		ws.onopen = () => {
			this.#attempt = 0
			this.socketReason = ''
			const device = this.devices.find((entry) => entry.id === this.deviceId)
			ws.send(
				JSON.stringify({
					sample_rate: Math.round(this.#context?.sampleRate ?? 48000),
					channels: 1,
					label: device?.label ?? '',
				}),
			)
			this.socket = 'open'
		}
		ws.onclose = (event) => {
			this.socket = 'closed'
			this.#ws = null
			if (this.#closing) return
			// Whatever the server said is the only part a GM can act on: another
			// tab holding the capture slot, or a login that has expired.
			this.socketReason = event.reason
			this.socket = 'connecting'
			this.#retry = setTimeout(() => this.#openSocket(), backoffDelay(this.#attempt))
			this.#attempt++
		}
		ws.onerror = () => ws.close()
	}

	async #takeWakeLock(): Promise<void> {
		const api = (
			navigator as Navigator & {
				wakeLock?: { request: (kind: string) => Promise<WakeLock> }
			}
		).wakeLock
		if (!api) {
			this.wakeLockHeld = false
			return
		}
		try {
			const lock = await api.request('screen')
			this.#wakeLock = lock
			this.wakeLockHeld = true
			lock.addEventListener('release', () => {
				this.wakeLockHeld = false
			})
		} catch {
			// Refused (battery saver, an unfocused tab). The card says the lid
			// has to stay open either way; this only decides whether it is a
			// promise or a warning.
			this.wakeLockHeld = false
		}
	}

	/** Re-take the lock after the tab comes back. A browser releases it every
	 *  time the tab is hidden, so without this it is held only until the first
	 *  time somebody switches tabs. */
	async refreshWakeLock(): Promise<void> {
		if (!this.streaming || this.wakeLockHeld) return
		if (document.visibilityState !== 'visible') return
		await this.#takeWakeLock()
	}

	async #releaseWakeLock(): Promise<void> {
		const lock = this.#wakeLock
		this.#wakeLock = null
		this.wakeLockHeld = false
		if (!lock || lock.released) return
		try {
			await lock.release()
		} catch {
			// Already gone; there is nothing to hold and nothing to report.
		}
	}
}

export const clientMic = new ClientMicStore()

/** Where the source choice is remembered. Deliberately this browser's storage
 *  and not the server's action defaults: "the box on the table records from
 *  its own microphone and my laptop records from mine" is two different right
 *  answers on one server, and a shared default would have them overwrite each
 *  other every session. It also keeps a headless start honest - nothing stored
 *  server-side can ever select a source that needs a browser. */
const SOURCE_KEY = 'loreline.capture.source'

export type CaptureSourceChoice = 'device' | 'client'

export function storedCaptureSource(): CaptureSourceChoice {
	if (typeof localStorage === 'undefined') return 'device'
	return localStorage.getItem(SOURCE_KEY) === 'client' ? 'client' : 'device'
}

export function rememberCaptureSource(choice: CaptureSourceChoice): void {
	if (typeof localStorage === 'undefined') return
	localStorage.setItem(SOURCE_KEY, choice)
}
