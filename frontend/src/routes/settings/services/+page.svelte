<script lang="ts">
import { onDestroy, onMount } from 'svelte'
import { api, ApiError } from '$lib/api'
import { Button } from '$lib/components/ui/button'
import { Badge } from '$lib/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '$lib/components/ui/card'
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from '$lib/components/ui/table'
import { ScrollText } from '@lucide/svelte'
import type { ServiceState } from '$lib/types'

let services = $state<ServiceState[]>([])
let error = $state('')
let busy = $state<Record<string, boolean>>({})
let logsFor = $state('')
let logs = $state('')
let logsLoading = $state(false)
let timer: ReturnType<typeof setInterval> | null = null

// The app and the docker-proxy are what make this page work at all - showing
// them separately from the optional services makes it obvious why they have
// no start/stop control.
const core = $derived(services.filter((s) => !s.controllable))
const optional = $derived(services.filter((s) => s.controllable))

async function load() {
	try {
		services = await api.listServices()
		error = ''
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'failed to load services'
	}
}

async function toggle(svc: ServiceState) {
	busy = { ...busy, [svc.name]: true }
	try {
		const updated = await api.setServiceRunning(svc.name, svc.state !== 'running')
		services = services.map((s) => (s.name === updated.name ? updated : s))
		error = ''
	} catch (err) {
		error = err instanceof ApiError ? err.message : `failed to update ${svc.name}`
	} finally {
		busy = { ...busy, [svc.name]: false }
	}
}

async function showLogs(name: string) {
	logsFor = name
	logsLoading = true
	logs = ''
	try {
		logs = (await api.serviceLogs(name)).logs || '(no output)'
	} catch (err) {
		logs = err instanceof ApiError ? err.message : 'failed to fetch logs'
	} finally {
		logsLoading = false
	}
}

onMount(() => {
	load()
	timer = setInterval(load, 5000)
})
onDestroy(() => timer && clearInterval(timer))
</script>

{#snippet row(svc: ServiceState)}
	<TableRow>
		<TableCell class="font-medium">{svc.name}</TableCell>
		<TableCell>
			{#if svc.state === 'running'}
				<Badge variant="secondary" class="gap-1.5">
					<span class="size-2 rounded-full bg-emerald-500"></span>running
				</Badge>
			{:else}
				<Badge variant="outline" class="gap-1.5">
					<span class="size-2 rounded-full bg-muted-foreground"></span>{svc.state}
				</Badge>
			{/if}
		</TableCell>
		<TableCell class="text-muted-foreground">{svc.status}</TableCell>
		<TableCell class="text-muted-foreground"><code class="text-xs">{svc.image}</code></TableCell>
		<TableCell>
			<div class="flex justify-end gap-1">
				<Button variant="ghost" size="icon-sm" title="Logs" onclick={() => showLogs(svc.name)}>
					<ScrollText />
				</Button>
				{#if svc.controllable}
					<Button variant="outline" size="sm" disabled={busy[svc.name]} onclick={() => toggle(svc)}>
						{svc.state === 'running' ? 'Stop' : 'Start'}
					</Button>
				{/if}
			</div>
		</TableCell>
	</TableRow>
{/snippet}

{#if error}
	<p class="mb-4 text-sm text-destructive">{error}</p>
{/if}

{#if services.length === 0 && !error}
	<Card>
		<CardContent class="py-6 text-sm text-muted-foreground">
			No services to show. This page needs the Docker API - it's wired up automatically in the
			Docker Compose deployment, and unavailable in a source install.
		</CardContent>
	</Card>
{:else}
	<Card>
		<CardHeader>
			<CardTitle>Core services</CardTitle>
			<CardDescription>
				Loreline itself and its supporting containers. Not stoppable from here - stopping the app
				would kill this page, and the proxy is what makes it work.
			</CardDescription>
		</CardHeader>
		<CardContent>
			<Table>
				<TableHeader>
					<TableRow>
						<TableHead>Service</TableHead><TableHead>State</TableHead><TableHead>Status</TableHead
						><TableHead>Image</TableHead><TableHead></TableHead>
					</TableRow>
				</TableHeader>
				<TableBody>
					{#each core as svc (svc.name)}
						{@render row(svc)}
					{/each}
				</TableBody>
			</Table>
		</CardContent>
	</Card>

	<Card class="mt-4">
		<CardHeader>
			<CardTitle>Additional services</CardTitle>
			<CardDescription>
				Optional self-hosted STT and diarization. Start them here when you want to run transcription
				locally instead of through a cloud provider.
			</CardDescription>
		</CardHeader>
		<CardContent>
			{#if optional.length === 0}
				<p class="m-0 text-sm text-muted-foreground">
					None installed. Create them once with
					<code>docker compose --profile local-stt --profile diarization create</code>, then they
					appear here to start and stop.
				</p>
			{:else}
				<Table>
					<TableHeader>
						<TableRow>
							<TableHead>Service</TableHead><TableHead>State</TableHead><TableHead>Status</TableHead
							><TableHead>Image</TableHead><TableHead></TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{#each optional as svc (svc.name)}
							{@render row(svc)}
						{/each}
					</TableBody>
				</Table>
			{/if}
		</CardContent>
	</Card>
{/if}

{#if logsFor}
	<Card class="mt-4">
		<CardHeader>
			<div class="flex items-center justify-between">
				<CardTitle>Logs - {logsFor}</CardTitle>
				<div class="flex gap-1">
					<Button variant="outline" size="sm" onclick={() => showLogs(logsFor)}>Refresh</Button>
					<Button variant="ghost" size="sm" onclick={() => (logsFor = '')}>Close</Button>
				</div>
			</div>
		</CardHeader>
		<CardContent>
			{#if logsLoading}
				<p class="m-0 text-sm text-muted-foreground">Loading…</p>
			{:else}
				<pre
					class="m-0 max-h-96 overflow-auto rounded-md bg-foreground/5 p-3 font-mono text-xs leading-relaxed"
				>{logs}</pre>
			{/if}
		</CardContent>
	</Card>
{/if}
