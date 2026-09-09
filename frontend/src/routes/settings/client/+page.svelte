<script lang="ts">
import { onDestroy, onMount } from 'svelte'
import { ApiError, api } from '$lib/api'
import { Button } from '$lib/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '$lib/components/ui/card'
import { Label } from '$lib/components/ui/label'
import { Separator } from '$lib/components/ui/separator'
import { Switch } from '$lib/components/ui/switch'
import Dropdown from '$lib/Dropdown.svelte'
import LevelMeter from '$lib/LevelMeter.svelte'
import type { InputDevice, RevisionResponse, UpdateResult } from '$lib/wire'

let devices = $state<InputDevice[]>([])
let deviceSel = $state('')
let deviceMsg = $state('')
let metering = $state(false)
let peak = $state(0)
let meterError = $state('')
let levelWs: WebSocket | null = null

let revision = $state<RevisionResponse | null>(null)
let updating = $state(false)
let updateResult = $state<UpdateResult | null>(null)
let autostart = $state<boolean | null>(null)
let autostartBusy = $state(false)
// Bumped every time a toggle attempt settles (success or failure), win or
// lose. The <Switch> below is keyed on it purely to force a remount on every
// settle - see the {#key} block for why that's needed even when `autostart`
// itself settles back to the value it started at.
let autostartGeneration = $state(0)
let opsMessage = $state('')

// A previously-picked device (e.g. a Bluetooth mic) can disappear from the
// device list without the stored selection changing - flagged here instead
// of leaving the user to find out only when the Test button fails with a raw
// PortAudio error.
const deviceMissing = $derived(deviceSel !== '' && !devices.some((d) => d.name === deviceSel))
// What the Revision row shows, in descending order of how much it tells a
// person: git's own name for this build (`v0.2.0-93-ge57029c`, or a bare short
// SHA where no tag is reachable), else ten characters of the commit, else a
// dash for a deployment that genuinely cannot know - a Docker image built
// without the revision baked in. The middle case is not hypothetical; see
// runUpdate for the one path that leaves only a SHA behind.
const revisionLabel = $derived(revision?.described || revision?.commit?.slice(0, 10) || '-')

const deviceOptions = $derived([
	{ value: '', label: 'System default' },
	...devices.map((d) => ({ value: d.name, label: d.name })),
	...(deviceMissing
		? [
				{
					value: deviceSel,
					label: `${deviceSel} (not found)`,
					disabled: true,
					title: 'This device is no longer available - pick another one.',
				},
			]
		: []),
])

// The <pre> lower down would otherwise repeat a single-line opsMessage
// verbatim (see runUpdate) - only what is left under the headline gets its own
// block. On a success that is the update script's notes, on a failure its whole
// transcript; either way, repeating the headline inside it would be noise.
let updateNotes = $state('')
const showUpdateOutput = $derived(updateNotes !== '')

async function loadDevices() {
	try {
		devices = await api.listDevices()
		deviceSel = (await api.getInputDevice()).device ?? ''
	} catch {
		devices = []
	}
}

async function saveDevice() {
	try {
		await api.setInputDevice(deviceSel || null)
		deviceMsg = 'Saved'
	} catch (err) {
		deviceMsg = err instanceof ApiError ? err.message : 'save failed'
	}
	if (metering) {
		stopMeter()
		startMeter()
	}
}

function toggleMeter() {
	if (metering) stopMeter()
	else startMeter()
}

function startMeter() {
	meterError = ''
	const q = deviceSel ? `?device=${encodeURIComponent(deviceSel)}` : ''
	const proto = location.protocol === 'https:' ? 'wss' : 'ws'
	levelWs = new WebSocket(`${proto}://${location.host}/ws/audio/level${q}`)
	metering = true
	levelWs.onmessage = (event) => {
		let data: { peak?: number; error?: string }
		try {
			data = JSON.parse(event.data) as { peak?: number; error?: string }
		} catch {
			// One malformed frame is not a reason to kill the meter.
			console.warn('audio level: malformed frame', event.data)
			return
		}
		if (data.error) {
			meterError = data.error
			stopMeter()
			return
		}
		if (typeof data.peak === 'number') peak = data.peak
	}
	levelWs.onclose = () => {
		metering = false
		peak = 0
		levelWs = null
	}
	levelWs.onerror = () => {
		metering = false
	}
}

function stopMeter() {
	levelWs?.close()
	levelWs = null
	metering = false
	peak = 0
}

async function loadOps() {
	try {
		revision = await api.revision()
	} catch {
		revision = null
	}
	try {
		autostart = (await api.getAutostart()).enabled
	} catch {
		autostart = null
	}
}

async function runUpdate() {
	opsMessage = ''
	updating = true
	try {
		updateResult = await api.update()
		// An update result carries the SHA and nothing else, so adopting it
		// wholesale would trade git's readable name for ten characters of hex.
		// Only do that when the commit actually moved - which in a Docker
		// deployment it does not, the value there being baked into an image
		// this update has not replaced yet.
		if (updateResult.new_commit !== revision?.commit) {
			revision = { commit: updateResult.new_commit, described: null }
		}
		// A single-line output is one clear sentence about the outcome, written
		// by the side that actually knows it: "not available in a Docker
		// deployment", or - once that deployment can hand the job to the
		// updater service - that there was nothing to update, or that a newer
		// image was pulled and the app is being recreated onto it. Show it
		// verbatim instead of a verdict this page would be guessing at, and
		// instead of pointing at the <pre> below, which a one-line output
		// doesn't render anyway. A failed update comes back as the update
		// script's own transcript, which is multi-line and does render there.
		// A successful update answers with its verdict on the first line and,
		// when the update script had something it wanted read, its notes below.
		// So the headline is that first line rather than a generic "Update
		// complete.", which would bury the one sentence written by the side
		// that actually knows what happened. A failure comes back as the
		// script's own transcript, where no line is a headline, so that keeps
		// the generic sentence and sends the reader to the block below.
		const [first = '', ...rest] = (updateResult.output ?? '').split('\n')
		if (updateResult.ok) {
			opsMessage = first || 'Update complete.'
			updateNotes = rest.join('\n').trim()
		} else {
			opsMessage = first && !rest.length ? first : 'Update failed (see output).'
			updateNotes = rest.length ? updateResult.output : ''
		}
	} catch (err) {
		opsMessage = err instanceof ApiError ? err.message : 'update failed'
	} finally {
		updating = false
	}
}

async function toggleAutostart() {
	if (autostart === null) return
	autostartBusy = true
	try {
		autostart = (await api.setAutostart(!autostart)).enabled
	} catch (err) {
		opsMessage = err instanceof ApiError ? err.message : 'autostart toggle failed'
	} finally {
		autostartBusy = false
		// The bits-ui Switch only resyncs its own optimistic state when the
		// `checked` prop's value changes - a failed toggle settles back to the
		// same value it started at, which it would otherwise never notice, and
		// it would keep showing "on" even though `autostart` is correctly
		// `false`. Bumping this every settle forces the {#key} below to remount
		// it regardless.
		autostartGeneration += 1
	}
}

onMount(async () => {
	await loadDevices()
	await loadOps()
})

onDestroy(stopMeter)
</script>

<Card>
	<CardHeader>
		<CardTitle>Client settings</CardTitle>
		<CardDescription>Microphone, autostart and self-update of this recorder.</CardDescription>
	</CardHeader>
	<CardContent class="flex flex-wrap items-end gap-6">
		<div class="flex min-w-60 flex-1 flex-col gap-2">
			<Label for="device">Microphone</Label>
			<Dropdown
				id="device"
				bind:value={deviceSel}
				options={deviceOptions}
				onpick={() => void saveDevice()}
			/>
		</div>
		<div class="flex min-w-60 flex-1 flex-col gap-2">
			<span class="text-sm text-muted-foreground">Input level</span>
			<div class="flex items-center gap-2">
				<Button variant="outline" onclick={toggleMeter}>{metering ? 'Stop' : 'Test'}</Button>
				<LevelMeter {peak} class="flex-1" />
			</div>
		</div>
	</CardContent>
	<CardContent class="flex flex-col gap-1 pt-0">
		{#if deviceMsg}
			<span class="text-sm text-muted-foreground">{deviceMsg}</span>
		{/if}
		{#if meterError}
			<p class="text-sm text-destructive">{meterError}</p>
		{/if}
		<p class="text-xs text-muted-foreground">Used for every session started from the Dashboard.</p>
		<Separator class="my-3" />
		<div class="flex items-center justify-between gap-2">
			<span class="shrink-0 text-muted-foreground">Revision</span>
			<div class="flex min-w-0 items-center gap-2">
				<!-- The described string runs to 40-odd characters and this row also
				     holds a button, so: one line, smaller type, and an ellipsis
				     rather than a wrap or a squeezed button. It fits whole at any
				     normal width; on a phone the tail is what gets cut, and the
				     title carries the full SHA, which names the build exactly
				     whatever is visible. -->
				<code class="min-w-0 truncate text-xs" title={revision?.commit}>{revisionLabel}</code>
				<Button variant="outline" size="sm" onclick={runUpdate} disabled={updating}>
					{updating ? 'Updating…' : 'Update now'}
				</Button>
			</div>
		</div>
		<div class="mt-2 flex items-center justify-between">
			<span class="text-muted-foreground">Autostart</span>
			{#if autostart === null}
				<span class="text-muted-foreground">unavailable</span>
			{:else}
				{#key autostartGeneration}
					<Switch checked={autostart} onCheckedChange={toggleAutostart} disabled={autostartBusy} />
				{/key}
			{/if}
		</div>
		{#if opsMessage}
			<p class="mt-2 text-sm text-muted-foreground">{opsMessage}</p>
		{/if}
		{#if showUpdateOutput}
			<pre class="mt-2 max-h-32 overflow-auto font-mono text-xs">{updateNotes}</pre>
		{/if}
	</CardContent>
</Card>
