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
let chanForm = $state<AlertChannelWrite>(blankChannel())
let chanSelected = $state<ChannelMeta | null>(null)
let chanStep = $state(1)
let chanEditing = $state<string | null>(null)
let chanOpen = $state(false)
let chanMsg = $state('')

async function loadChannels() {
	try {
		channels = await api.listAlertChannels()
	} catch {
		channels = []
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

function channelValid(): boolean {
	if (!chanSelected) return false
	if (chanSelected.type === 'ntfy') return !!chanForm.topic
	if (chanSelected.type === 'telegram') return !!chanForm.chat_id
	return !!chanForm.url
}

async function saveChannel() {
	chanMsg = ''
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
	}
}

async function testChannel(id: string) {
	chanMsg = ''
	try {
		chanMsg = (await api.testAlertChannel(id)).ok ? 'Test sent' : 'Test failed'
	} catch (err) {
		chanMsg = err instanceof ApiError ? err.message : 'test failed'
	}
}

async function deleteChannel(id: string) {
	if (!(await confirm({ description: 'Delete this alert channel?', destructive: true }))) return
	await api.deleteAlertChannel(id)
	await loadChannels()
}

onMount(loadChannels)
</script>

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
		<p class="px-6 text-sm text-muted-foreground">{chanMsg}</p>
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
				{#if channels.length === 0}
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
						<Input id="csrv" bind:value={chanForm.server} placeholder="https://ntfy.sh" />
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
						<Input id="curl" bind:value={chanForm.url} placeholder="https://…" />
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
