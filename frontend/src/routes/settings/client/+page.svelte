<script lang="ts">
import { onDestroy, onMount } from 'svelte'
import { ApiError, api } from '$lib/api'
import { Button } from '$lib/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '$lib/components/ui/card'
import { Label } from '$lib/components/ui/label'
import { Separator } from '$lib/components/ui/separator'
import { Switch } from '$lib/components/ui/switch'
import Dropdown from '$lib/Dropdown.svelte'
import type { InputDevice, UpdateResult } from '$lib/wire'

let devices = $state<InputDevice[]>([])
let deviceSel = $state('')
let deviceMsg = $state('')
let metering = $state(false)
let peak = $state(0)
let meterError = $state('')
let levelWs: WebSocket | null = null

let revision = $state<string | null>(null)
let updating = $state(false)
let updateResult = $state<UpdateResult | null>(null)
let autostart = $state<boolean | null>(null)
let autostartBusy = $state(false)
let opsMessage = $state('')

const meterColor = $derived(peak > 0.9 ? '#ef4444' : peak > 0.6 ? '#f59e0b' : '#22c55e')

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
		const data = JSON.parse(event.data) as { peak?: number; error?: string }
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
		revision = (await api.revision()).commit
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
		revision = updateResult.new_commit
		if (updateResult.ok) {
			opsMessage = 'Update complete.'
		} else {
			// A single-line output is one clear reason (e.g. "not available in a
			// Docker deployment") - show it directly instead of a generic
			// failure message plus a redundant pointer at the <pre> below.
			const single = updateResult.output && !updateResult.output.includes('\n')
			opsMessage = single ? updateResult.output : 'Update failed (see output).'
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
				options={[
          { value: '', label: 'System default' },
          ...devices.map((d) => ({ value: d.name, label: d.name }))
        ]}
				onpick={() => void saveDevice()}
			/>
		</div>
		<div class="flex min-w-60 flex-1 flex-col gap-2">
			<span class="text-sm text-muted-foreground">Input level</span>
			<div class="flex items-center gap-2">
				<Button variant="outline" onclick={toggleMeter}>{metering ? 'Stop' : 'Test'}</Button>
				<div class="h-2.5 flex-1 overflow-hidden rounded-full bg-foreground/15">
					<div
						class="h-full rounded-full transition-[width] duration-75"
						style="width: {Math.min(100, Math.round(peak * 100))}%; background: {meterColor};"
					></div>
				</div>
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
		<div class="flex items-center justify-between">
			<span class="text-muted-foreground">Revision</span>
			<div class="flex items-center gap-2">
				<code>{revision ? revision.slice(0, 10) : '-'}</code>
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
				<Switch checked={autostart} onCheckedChange={toggleAutostart} disabled={autostartBusy} />
			{/if}
		</div>
		{#if opsMessage}
			<p class="mt-2 text-sm text-muted-foreground">{opsMessage}</p>
		{/if}
		{#if updateResult?.output}
			<pre class="mt-2 max-h-32 overflow-auto font-mono text-xs">{updateResult.output}</pre>
		{/if}
	</CardContent>
</Card>
