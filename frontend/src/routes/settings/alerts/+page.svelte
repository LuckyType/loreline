<script lang="ts">
import { Pencil, Plus, Send, Trash2 } from '@lucide/svelte'
import { onMount } from 'svelte'
import { ApiError, api } from '$lib/api'
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
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '$lib/components/ui/dialog'
import { Input } from '$lib/components/ui/input'
import { Label } from '$lib/components/ui/label'
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from '$lib/components/ui/table'
import { confirm } from '$lib/confirm.svelte'
import Dropdown from '$lib/Dropdown.svelte'
import type { AlertChannel, AlertChannelKind, AlertChannelWrite, AlertLevelKind } from '$lib/wire'

interface ChannelMeta {
	type: AlertChannelKind
	label: string
	note: string
	fields: ('server' | 'topic' | 'chat_id' | 'url' | 'token')[]
	tokenLabel?: string
}

const CHANNELS: ChannelMeta[] = [
	{
		type: 'ntfy',
		label: 'ntfy',
		note: 'Push to ntfy.sh or a self-hosted ntfy server.',
		fields: ['server', 'topic', 'token'],
		tokenLabel: 'Auth token (optional)',
	},
	{
		type: 'telegram',
		label: 'Telegram',
		note: 'Send a bot message to a chat.',
		fields: ['chat_id', 'token'],
		tokenLabel: 'Bot token',
	},
	{
		type: 'webhook',
		label: 'Webhook',
		note: 'POST a JSON payload to a URL.',
		fields: ['url'],
	},
]

const LEVELS: AlertLevelKind[] = ['info', 'warning', 'error']

const blankChannel = (): AlertChannelWrite => ({
	type: 'ntfy',
	enabled: true,
	min_level: 'warning',
	server: 'https://ntfy.sh',
	topic: '',
	chat_id: '',
	url: '',
	token: '',
})

let channels = $state<AlertChannel[]>([])
/** What the last load ran into, or '' when the table is what the server
 *  said. A failed load used to become an empty list, which the table renders
 *  as "no channels configured": the wrong answer, and one nobody could tell
 *  apart from the right one. */
let loadError = $state('')
let chanForm = $state<AlertChannelWrite>(blankChannel())
let chanSelected = $state<ChannelMeta | null>(null)
let chanStep = $state(1)
let chanEditing = $state<string | null>(null)
let chanOpen = $state(false)
/**
 * The line above the table: a short verdict, and the reason behind a failure.
 *
 * Split in two because they are read differently. `chanMsg` is the three-word
 * answer ("Test sent", "Test failed"); `chanDetail` is what the channel
 * actually said, which is the difference between "Test failed" and "HTTP 401:
 * Unauthorized" - one says something is wrong, the other says which field to
 * fix. The server scrubs the channel's token out of it before it gets here.
 */
let chanMsg = $state('')
let chanDetail = $state('')

/**
 * A vendor can answer a failed POST with an entire HTML error page, and this
 * message sits inside a card header. Show the front of it and hang the rest
 * off the tooltip, the way the provider table carries the vendor's own
 * sentence on its health badge.
 */
const DETAIL_CHARS = 120
const shortDetail = $derived(
	chanDetail.length > DETAIL_CHARS ? `${chanDetail.slice(0, DETAIL_CHARS - 1)}…` : chanDetail,
)
// One string rather than a verdict element and a reason element: the two are
// separated by a colon with no space before it, and markup that has to sit on
// one line to keep it that way is markup the formatter will eventually break.
const chanLine = $derived(chanDetail ? `${chanMsg}: ${shortDetail}` : chanMsg)

/**
 * Whether a string is something the backend could actually POST to.
 *
 * Deliberately the same test the API applies (an absolute http or https URL,
 * see `_absolute_http_url` in `web/schemas.py`) and no stricter: the point is
 * to catch `not-a-url` at the field, not to second-guess a LAN hostname or a
 * receiver that happens to be down while the channel is being set up.
 */
function isHttpUrl(value: string | null | undefined): boolean {
	if (!value?.trim()) return false
	try {
		const { protocol } = new URL(value.trim())
		return protocol === 'http:' || protocol === 'https:'
	} catch {
		return false
	}
}

// Shown under the field itself rather than only as a disabled button, so the
// answer to "why can I not save this" is next to the thing that is wrong.
// Blank while the field is still empty: a form you have not filled in yet is
// not a form you got wrong.
const URL_HINT = 'Enter an absolute URL, for example https://hooks.example/loreline'
const serverError = $derived(
	chanSelected?.type === 'ntfy' && chanForm.server && !isHttpUrl(chanForm.server) ? URL_HINT : '',
)
const urlError = $derived(
	chanSelected?.type === 'webhook' && chanForm.url && !isHttpUrl(chanForm.url) ? URL_HINT : '',
)

async function loadChannels() {
	loadError = ''
	try {
		channels = await api.listAlertChannels()
	} catch (err) {
		// The last good list stays up under the banner rather than vanishing.
		loadError = `Could not load the channels: ${
			err instanceof ApiError ? err.message : 'the request failed'
		}`
	}
}

function openChannelWizard() {
	chanEditing = null
	chanSelected = null
	chanForm = blankChannel()
	chanStep = 1
	chanOpen = true
}

function pickChannelType(meta: ChannelMeta) {
	chanSelected = meta
	chanForm = { ...blankChannel(), type: meta.type }
	chanStep = 2
}

function resetChannelWizard() {
	chanEditing = null
	chanSelected = null
	chanForm = blankChannel()
	chanStep = 1
	chanOpen = false
}

function editChannel(c: AlertChannel) {
	chanEditing = c.id
	chanSelected = CHANNELS.find((m) => m.type === c.type) ?? null
	chanForm = {
		type: c.type,
		enabled: c.enabled,
		min_level: c.min_level,
		server: c.server,
		topic: c.topic ?? '',
		chat_id: c.chat_id ?? '',
		url: c.url ?? '',
		token: '',
	}
	chanStep = 2
	chanOpen = true
}

/**
 * Whether the form describes a channel worth saving.
 *
 * Presence was the whole gate, and truthiness let `not-a-url` through: the
 * channel saved, sat in the table looking configured, and delivered nothing.
 * The URL-shaped fields now get the shape check on top of the presence check,
 * which is the same one the API enforces - so a client that skips it is
 * refused rather than obeyed, and this only decides whether the user finds out
 * at the field or at the save.
 */
function channelValid(): boolean {
	if (!chanSelected) return false
	if (chanSelected.type === 'ntfy') return !!chanForm.topic && isHttpUrl(chanForm.server)
	if (chanSelected.type === 'telegram') return !!chanForm.chat_id
	return isHttpUrl(chanForm.url)
}

async function saveChannel() {
	chanMsg = ''
	chanDetail = ''
	const body: AlertChannelWrite = {
		...chanForm,
		topic: chanForm.topic || null,
		chat_id: chanForm.chat_id || null,
		url: chanForm.url || null,
		token: chanForm.token || null,
	}
	try {
		if (chanEditing) await api.updateAlertChannel(chanEditing, body)
		else await api.createAlertChannel(body)
		resetChannelWizard()
		await loadChannels()
	} catch (err) {
		chanMsg = err instanceof ApiError ? err.message : 'save failed'
	}
}

async function toggleChannel(c: AlertChannel) {
	try {
		await api.updateAlertChannel(c.id, {
			type: c.type,
			enabled: !c.enabled,
			min_level: c.min_level,
			server: c.server,
			topic: c.topic,
			chat_id: c.chat_id,
			url: c.url,
		})
		await loadChannels()
	} catch (err) {
		chanMsg = err instanceof ApiError ? err.message : 'update failed'
		chanDetail = ''
	}
}

async function testChannel(id: string) {
	chanMsg = ''
	chanDetail = ''
	try {
		// A refused channel is still a successful request, so the reason arrives
		// in the body rather than as a thrown ApiError. The catch below is for
		// the test route itself being unreachable, which says nothing about the
		// channel.
		const result = await api.testAlertChannel(id)
		chanMsg = result.ok ? 'Test sent' : 'Test failed'
		chanDetail = result.ok ? '' : (result.detail ?? '')
	} catch (err) {
		chanMsg = err instanceof ApiError ? err.message : 'test failed'
	}
}

async function deleteChannel(id: string) {
	chanMsg = ''
	chanDetail = ''
	if (!(await confirm({ description: 'Delete this alert channel?', destructive: true }))) return
	try {
		await api.deleteAlertChannel(id)
		await loadChannels()
	} catch (err) {
		// The row is still in the table, so the line above it has to say why,
		// in the same verdict-and-reason shape as a failed test.
		chanMsg = 'Delete failed'
		chanDetail = err instanceof ApiError ? err.message : 'the request failed'
	}
}

onMount(loadChannels)
</script>

{#if loadError}
	<!-- Same banner and retry as the providers page: the table below renders
	     "No alert channels" the moment the list is empty, which is not what a
	     failed load means. -->
	<div
		class="mb-4 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
	>
		<span>{loadError}</span>
		<button class="underline underline-offset-2" onclick={loadChannels}>Retry</button>
	</div>
{/if}

<Card>
	<CardHeader>
		<CardTitle>Push alerts</CardTitle>
		<CardDescription>
			Channels (ntfy, Telegram, webhook) notified about failures and session events.
		</CardDescription>
		<CardAction>
			<Button
				variant="outline"
				size="icon-sm"
				title="Add channel"
				aria-label="Add channel"
				onclick={openChannelWizard}
			>
				<Plus />
			</Button>
		</CardAction>
	</CardHeader>
	{#if chanMsg}
		<!-- The full reason rides on the tooltip, the way the provider table
		     carries the vendor's sentence on its health badge: the line stays one
		     line, and nothing is thrown away. -->
		<p class="px-6 text-sm break-words text-muted-foreground" title={chanDetail || undefined}>
			{chanLine}
		</p>
	{/if}
	<CardContent class="pt-0">
		<Table>
			<TableHeader>
				<TableRow>
					<TableHead>Type</TableHead><TableHead>Target</TableHead><TableHead>Min level</TableHead
					><TableHead>On</TableHead><TableHead></TableHead>
				</TableRow>
			</TableHeader>
			<TableBody>
				{#each channels as c (c.id)}
					<TableRow>
						<TableCell>{c.type}</TableCell>
						<TableCell class="text-muted-foreground"
							>{c.type === 'telegram'
                ? c.chat_id
                : c.type === 'webhook'
                  ? c.url
                  : c.topic}</TableCell
						>
						<TableCell>{c.min_level}</TableCell>
						<TableCell>
							<Checkbox
								checked={c.enabled}
								onCheckedChange={() => toggleChannel(c)}
								aria-label="Enabled"
							/>
						</TableCell>
						<TableCell>
							<div class="flex gap-1">
								<Button
									variant="ghost"
									size="icon-sm"
									title="Send test"
									aria-label="Send test"
									onclick={() => testChannel(c.id)}
								>
									<Send />
								</Button>
								<Button
									variant="ghost"
									size="icon-sm"
									title="Edit"
									aria-label="Edit"
									onclick={() => editChannel(c)}
								>
									<Pencil />
								</Button>
								<Button
									variant="ghost"
									size="icon-sm"
									title="Delete"
									aria-label="Delete"
									onclick={() => deleteChannel(c.id)}
								>
									<Trash2 />
								</Button>
							</div>
						</TableCell>
					</TableRow>
				{/each}
				{#if channels.length === 0 && !loadError}
					<TableRow>
						<TableCell colspan={5} class="text-muted-foreground"
							>No alert channels - click + to add one.</TableCell
						>
					</TableRow>
				{/if}
			</TableBody>
		</Table>
	</CardContent>
</Card>

<Dialog bind:open={chanOpen}>
	<DialogContent class="sm:max-w-md">
		<DialogHeader>
			<DialogTitle>{chanEditing ? 'Edit channel' : 'Add channel'}</DialogTitle>
		</DialogHeader>

		{#if chanStep === 1}
			<DialogDescription>Choose a channel type</DialogDescription>
			<div class="mt-2 flex flex-col gap-1.5">
				{#each CHANNELS as meta (meta.type)}
					<Button
						variant="outline"
						class="h-auto flex-col items-start gap-0.5 py-2"
						onclick={() => pickChannelType(meta)}
					>
						<strong>{meta.label}</strong>
						<span class="text-xs font-normal text-muted-foreground">{meta.note}</span>
					</Button>
				{/each}
			</div>
		{:else if chanSelected}
			{@const meta = chanSelected}
			<div class="flex items-center justify-between">
				<strong>{meta.label}</strong>
				{#if !chanEditing}
					<Button variant="outline" size="sm" onclick={() => (chanStep = 1)}>← Back</Button>
				{/if}
			</div>
			<div class="mt-2 flex flex-col gap-4">
				{#if meta.fields.includes('server')}
					<div class="flex flex-col gap-2">
						<Label for="csrv">Server</Label>
						<Input
							id="csrv"
							bind:value={chanForm.server}
							placeholder="https://ntfy.sh"
							aria-invalid={!!serverError}
							aria-describedby={serverError ? 'csrv-error' : undefined}
						/>
						{#if serverError}
							<p id="csrv-error" class="text-sm text-destructive">{serverError}</p>
						{/if}
					</div>
				{/if}
				{#if meta.fields.includes('topic')}
					<div class="flex flex-col gap-2">
						<Label for="ctop">Topic</Label>
						<Input id="ctop" bind:value={chanForm.topic} placeholder="loreline-alerts" />
					</div>
				{/if}
				{#if meta.fields.includes('chat_id')}
					<div class="flex flex-col gap-2">
						<Label for="cchat">Chat id</Label>
						<Input id="cchat" bind:value={chanForm.chat_id} placeholder="123456789" />
					</div>
				{/if}
				{#if meta.fields.includes('url')}
					<div class="flex flex-col gap-2">
						<Label for="curl">URL</Label>
						<Input
							id="curl"
							bind:value={chanForm.url}
							placeholder="https://…"
							aria-invalid={!!urlError}
							aria-describedby={urlError ? 'curl-error' : undefined}
						/>
						{#if urlError}
							<p id="curl-error" class="text-sm text-destructive">{urlError}</p>
						{/if}
					</div>
				{/if}
				{#if meta.fields.includes('token')}
					<div class="flex flex-col gap-2">
						<Label for="ctok">{meta.tokenLabel}{chanEditing ? ' - blank = keep' : ''}</Label>
						<Input
							id="ctok"
							type="password"
							autocomplete="off"
							bind:value={chanForm.token}
							placeholder={chanEditing ? '•••• unchanged' : ''}
						/>
					</div>
				{/if}
				<div class="flex flex-col gap-2">
					<Label for="cmin">Min level</Label>
					<Dropdown
						id="cmin"
						bind:value={chanForm.min_level}
						options={LEVELS.map((level) => ({ value: level, label: level }))}
					/>
				</div>
				<label class="flex items-center gap-2">
					<Checkbox bind:checked={chanForm.enabled} />
					<span>Enabled</span>
				</label>
			</div>
			<DialogFooter>
				<Button variant="outline" onclick={resetChannelWizard}>Cancel</Button>
				<Button onclick={saveChannel} disabled={!channelValid()}>
					{chanEditing ? 'Save changes' : 'Add channel'}
				</Button>
			</DialogFooter>
		{/if}
	</DialogContent>
</Dialog>
