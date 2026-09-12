<script lang="ts">
import { ScrollText } from '@lucide/svelte'
import { onDestroy, onMount } from 'svelte'
import { ApiError, api } from '$lib/api'
import { Badge } from '$lib/components/ui/badge'
import { Button } from '$lib/components/ui/button'
import {
	Card,
	CardAction,
	CardContent,
	CardDescription,
	CardHeader,
	CardTitle,
} from '$lib/components/ui/card'
import { Checkbox } from '$lib/components/ui/checkbox'
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from '$lib/components/ui/table'
import Dropdown from '$lib/Dropdown.svelte'
import type { ServiceState } from '$lib/wire'

let services = $state<ServiceState[]>([])
let error = $state('')
let busy = $state<Record<string, boolean>>({})
let logsFor = $state('')
let logs = $state('')
let logsLoading = $state(false)
let timer: ReturnType<typeof setInterval> | null = null

// How many lines to ask the daemon for. Adjustable because the default is not
// enough on a chatty container: the app probes the diarizer's /healthz every
// few seconds, so 200 lines of that service's log covers a quarter of an hour
// and holds nothing else at all.
const TAIL_CHOICES = [200, 1000, 5000]
let tail = $state(TAIL_CHOICES[0])

// The other half of the same problem, and the reason this defaults to on: the
// probe lines are the ones nobody opens this panel to read, and dropping them
// is what makes the handful of lines that matter visible without scrolling
// past a wall of them. The count below always states the whole, so nothing is
// hidden silently.
let hideProbes = $state(true)

/** A health-probe line, which every service here answers many times a minute. */
function isProbe(line: string): boolean {
	return line.includes('/healthz') || line.includes('/livez')
}

const logLines = $derived(logs ? logs.split('\n') : [])
const shownLines = $derived(hideProbes ? logLines.filter((l) => !isProbe(l)) : logLines)
const hiddenCount = $derived(logLines.length - shownLines.length)
const hiddenNote = $derived(
	`${shownLines.length} of ${logLines.length} lines shown, ${hiddenCount} health ${
		hiddenCount === 1 ? 'probe' : 'probes'
	} hidden.`,
)

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
		// The row still shows the state it had before the press, and that can be
		// stale: a stop the server gave up waiting on has usually finished by
		// the time its error arrives here. Ask again now rather than leave a
		// container labelled running until the next poll, and keep the message
		// above the table, which load() would clear.
		try {
			services = await api.listServices()
		} catch {
			/* the poll retries */
		}
	} finally {
		busy = { ...busy, [svc.name]: false }
	}
}

async function showLogs(name: string) {
	logsFor = name
	logsLoading = true
	logs = ''
	try {
		logs = (await api.serviceLogs(name, tail)).logs || '(no output)'
	} catch (err) {
		logs = err instanceof ApiError ? err.message : 'failed to fetch logs'
	} finally {
		logsLoading = false
	}
}

/** Re-fetch at the new depth, but only while a log is actually on screen: this
 *  is a control on the open card, not a page-wide setting. */
function setTail(value: string) {
	tail = Number(value)
	if (logsFor) void showLogs(logsFor)
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
	<!-- The empty state is two different facts wearing one shape, and the old
	     wording knew only one of them: an app with no Docker API at all, and an
	     app with one that matched no container. It also read as "you are not on
	     Docker", which is wrong for the single container this page is now most
	     likely to be opened from. So it names what is missing, what it would
	     have been for, and the one thing to check in the other case. -->
	<Card>
		<CardContent class="py-6 text-sm text-muted-foreground">
			<p class="m-0">
				No containers to show. This page manages this stack's own containers, and it needs a Docker
				API to do that: <code>LORELINE_DOCKER_API</code>, which the appliance stack in
				<code>docker-compose.yml</code>
				points at a socket proxy that can only list containers, read their logs and start or stop
				them.
			</p>
			<p class="m-0 mt-2">
				A single container started with <code>docker run</code>, and a source install, have neither,
				and nothing else in Loreline needs one: cloud transcription, import, campaigns and exports
				all work without this page. Self-hosted STT and diarization are started from the host there
				instead.
			</p>
			<p class="m-0 mt-2">
				If this is the appliance stack and the list is still empty, its containers are running under
				a different compose project name than the one this app looks for (<code
					>COMPOSE_PROJECT_NAME</code
				>, "loreline" by default).
			</p>
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
			<CardTitle>Logs - {logsFor}</CardTitle>
			<CardDescription>
				Recent container output, newest last.
				{#if !logsLoading && hiddenCount > 0}
					{hiddenNote}
				{/if}
			</CardDescription>
			<CardAction class="flex flex-wrap items-center gap-2">
				<label class="flex items-center gap-2 text-sm">
					<Checkbox
						checked={hideProbes}
						onCheckedChange={(v) => (hideProbes = v === true)}
						aria-label="Hide health checks"
					/>
					<span class="text-muted-foreground">Hide health checks</span>
				</label>
				<Dropdown
					class="w-30"
					value={String(tail)}
					onpick={setTail}
					options={TAIL_CHOICES.map((n) => ({ value: String(n), label: `${n} lines` }))}
				/>
				<Button variant="outline" size="sm" onclick={() => showLogs(logsFor)}>Refresh</Button>
				<Button variant="ghost" size="sm" onclick={() => (logsFor = '')}>Close</Button>
			</CardAction>
		</CardHeader>
		<CardContent>
			{#if logsLoading}
				<p class="m-0 text-sm text-muted-foreground">Loading…</p>
			{:else if shownLines.length === 0}
				<p class="m-0 text-sm text-muted-foreground">
					{logLines.length === 0
						? '(no output)'
						: 'Every line in this range is a health probe. Untick "Hide health checks" to see them, or ask for more lines.'}
				</p>
			{:else}
				<pre
					class="m-0 max-h-96 overflow-auto rounded-md bg-foreground/5 p-3 font-mono text-xs leading-relaxed"
				>{shownLines.join('\n')}</pre>
			{/if}
		</CardContent>
	</Card>
{/if}
