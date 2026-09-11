<script lang="ts">
/**
 * One campaign: its sessions in order, and everything that has accumulated
 * about them.
 *
 * The order of the cards is the order the questions get asked. What happened,
 * and when - the sessions, oldest first, each saying whether it has a recap
 * yet, because a missing recap is the one gap in this page anybody can fill.
 * Then "where was it said", which is the search, scoped to this campaign and
 * linking into the transcript at the line. Then what to read aloud before the
 * next session. Then the cast, merged across every session that named anyone,
 * with the names worth biasing recognition towards tickable into the glossary
 * below it. The settings are last because they are answered once, and the cast
 * lives among them for the same reason: a table's players are typed in once and
 * then read by every session it records.
 *
 * Everything here is per campaign and nothing is shared with the History page,
 * which answers a different question ("what did I record last") in the
 * opposite order.
 */

import { ArrowDown, ArrowUp, Plus, Trash2 } from '@lucide/svelte'
import { onMount } from 'svelte'
import { goto } from '$app/navigation'
import { page } from '$app/state'
import { actionSetup } from '$lib/actionSetup.svelte'
import { ApiError, api } from '$lib/api'
import { campaigns } from '$lib/campaigns.svelte'
import { Badge } from '$lib/components/ui/badge'
import { Button } from '$lib/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '$lib/components/ui/card'
import { Input } from '$lib/components/ui/input'
import { Label } from '$lib/components/ui/label'
import { Textarea } from '$lib/components/ui/textarea'
import { confirm } from '$lib/confirm.svelte'
import ExtractedNames from '$lib/ExtractedNames.svelte'
import GenerateDialog from '$lib/GenerateDialog.svelte'
import GlossaryEditor from '$lib/GlossaryEditor.svelte'
import Markdown from '$lib/Markdown.svelte'
import { fmtWhen } from '$lib/stores'
import type {
	Campaign,
	CampaignDocument,
	CampaignEntities,
	CampaignPlayer,
	GenerateRequest,
	SearchHit,
	Session,
	SessionDocument,
} from '$lib/wire'

const id = $derived(page.params.id ?? '')

let campaign = $state<Campaign | null>(null)
let sessions = $state<Session[]>([])
let recaps = $state<SessionDocument[]>([])
let entities = $state<CampaignEntities | null>(null)
let previouslyOn = $state<CampaignDocument | null>(null)
let error = $state('')
let busy = $state(false)

// Bumped when the extracted-names list writes to the glossary, so the editor
// below re-reads instead of showing - and then saving back - the list as it
// was before the append.
let glossaryToken = $state(0)

// --- search ---
let query = $state('')
let hits = $state<SearchHit[]>([])
let indexed = $state(true)
let searching = $state(false)
let searched = $state(false)

// --- settings draft ---
let name = $state('')
let notes = $state('')
let recapPrompt = $state('')
let savedMessage = $state('')

// The cast, as rows being edited. A copy of what the campaign holds rather
// than a view onto it, like every other field in this block: nothing is stored
// until Save, and the row objects are what the each block is keyed on, so they
// survive a reorder without the inputs losing focus.
let cast = $state<CampaignPlayer[]>([])

/** Move a row one place, which is a change in priority: the cast leads the
 *  glossary, and a provider's ceiling is spent from the top of it. */
function moveCast(from: number, to: number) {
	if (to < 0 || to >= cast.length) return
	const [row] = cast.splice(from, 1)
	if (row) cast.splice(to, 0, row)
}

let previouslyOnOpen = $state(false)

const recapBySession = $derived(new Map(recaps.map((d) => [d.session_id, d])))
const llmProviders = $derived(actionSetup.providersFor('summarize'))

/** The six groups, in the order a GM reads them. Empty ones are dropped by the
 *  list itself, so a campaign whose sessions mentioned no factions shows five. */
const nameGroups = $derived(
	entities
		? [
				{ label: 'Characters', entries: entities.characters },
				{ label: 'Places', entries: entities.places },
				{ label: 'Items', entries: entities.items },
				{ label: 'Factions', entries: entities.factions },
				{ label: 'Quests', entries: entities.quests },
				{ label: 'Decisions', entries: entities.decisions },
			]
		: [],
)

async function load() {
	error = ''
	try {
		// One await, so a page that is going to render is fetched in one go
		// rather than in four visible steps.
		const [row, rows, documents, merged, opener] = await Promise.all([
			api.getCampaign(id),
			api.campaignSessions(id),
			api.campaignDocuments(id, 'recap'),
			api.campaignEntities(id),
			api.getPreviouslyOn(id),
		])
		campaign = row
		sessions = rows
		recaps = documents
		entities = merged
		previouslyOn = opener
		name = row.name
		notes = row.notes
		recapPrompt = row.recap_prompt
		cast = row.players.map((player) => ({ ...player }))
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'failed to load the campaign'
	}
}

async function search() {
	const q = query.trim()
	if (!q) {
		hits = []
		searched = false
		return
	}
	searching = true
	try {
		const results = await api.search(q, id)
		hits = results.hits
		indexed = results.indexed
		searched = true
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'search failed'
	} finally {
		searching = false
	}
}

async function save() {
	if (!campaign || !name.trim()) return
	busy = true
	error = ''
	try {
		// A row with neither name is dropped rather than sent: the backend
		// refuses it, and an empty row is what a GM leaves behind after pressing
		// Add and changing their mind, not something to make them fix.
		const players = cast
			.map((row) => ({ player: row.player.trim(), character: row.character.trim() }))
			.filter((row) => row.player || row.character)
		campaign = await api.updateCampaign(id, {
			name: name.trim(),
			notes,
			recap_prompt: recapPrompt,
			players,
		})
		cast = campaign.players.map((player) => ({ ...player }))
		await campaigns.reload()
		savedMessage = 'Saved'
		setTimeout(() => (savedMessage = ''), 2500)
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'could not save the campaign'
	} finally {
		busy = false
	}
}

async function remove() {
	if (!campaign) return
	const count = sessions.length
	const ok = await confirm({
		title: `Delete ${campaign.name}?`,
		// The sentence that matters: nobody is afraid of losing a name, they are
		// afraid of losing the recordings under it.
		description: `Its glossary and its "previously on" go with it. ${
			count === 1 ? 'Its 1 session is' : `Its ${count} sessions are`
		} kept, with no campaign.`,
		confirmLabel: 'Delete',
		destructive: true,
	})
	if (!ok) return
	busy = true
	try {
		await api.deleteCampaign(id)
		await campaigns.reload()
		goto('/campaigns')
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'could not delete the campaign'
		busy = false
	}
}

async function writePreviouslyOn(body: GenerateRequest) {
	previouslyOn = await api.writePreviouslyOn(id, { ...body, sessions: 3 })
}

onMount(() => {
	void actionSetup.load()
	void campaigns.load()
	void load()
})
</script>

{#if error}
	<p class="mb-4 text-sm text-destructive">{error}</p>
{/if}

{#if !campaign}
	<p class="text-muted-foreground">Loading…</p>
{:else}
	<div class="flex flex-col gap-4">
		<Card>
			<CardHeader>
				<CardTitle>{campaign.name}</CardTitle>
				<CardDescription>
					{sessions.length === 1 ? '1 session' : `${sessions.length} sessions`}
					{#if campaign.notes}
						· {campaign.notes}
					{/if}
				</CardDescription>
			</CardHeader>
			<CardContent class="flex flex-col gap-1">
				{#each sessions as session, i (session.id)}
					{@const recap = recapBySession.get(session.id)}
					<div class="flex flex-wrap items-center gap-2 border-b border-dashed py-1 last:border-0">
						<span class="w-8 text-muted-foreground">{i + 1}.</span>
						<a class="text-primary hover:underline" href="/sessions/{session.id}">
							{fmtWhen(session.started_at)}
						</a>
						<Badge variant={session.status === 'error' ? 'destructive' : 'secondary'}>
							{session.status}
						</Badge>
						<!-- The one gap on this page anybody can close, so it is said per
						     session rather than counted once at the top. -->
						{#if recap}
							<span class="text-xs text-muted-foreground">recap</span>
						{:else if session.summary}
							<span class="text-xs text-muted-foreground">summary only</span>
						{:else}
							<span class="text-xs text-muted-foreground">no recap yet</span>
						{/if}
					</div>
				{/each}
				{#if sessions.length === 0}
					<p class="m-0 text-muted-foreground">
						No sessions yet. Pick this campaign on the Dashboard before you start one, or move an
						existing session into it from its own page.
					</p>
				{/if}
			</CardContent>
		</Card>

		<Card>
			<CardHeader>
				<CardTitle>Search</CardTitle>
				<CardDescription>Find a line somebody said, across this campaign.</CardDescription>
			</CardHeader>
			<CardContent class="flex flex-col gap-2">
				<div class="flex gap-2">
					<Input
						bind:value={query}
						placeholder="A name, a place, a phrase"
						aria-label="Search this campaign"
						onkeydown={(e: KeyboardEvent) => {
							if (e.key === 'Enter') search()
						}}
					/>
					<Button onclick={search} disabled={searching || !query.trim()}>
						{searching ? 'Searching…' : 'Search'}
					</Button>
				</div>
				{#if !indexed}
					<!-- The hits are real and the order is not, which is invisible on a
					     campaign of three sessions and decides the answer on thirty. -->
					<p class="m-0 text-xs text-muted-foreground">
						This SQLite build has no full-text index, so results are listed newest first rather than
						by relevance.
					</p>
				{/if}
				{#each hits as hit (hit.session_id + hit.version + hit.start_ts)}
					<a
						class="flex flex-col gap-0.5 rounded-lg border px-3 py-2 hover:bg-accent"
						href="/sessions/{hit.session_id}?v={encodeURIComponent(hit.version)}&t={hit.start_ts}"
					>
						<span class="text-xs text-muted-foreground">
							{fmtWhen(hit.started_at)}
							{#if hit.speaker}
								· {hit.speaker}
							{/if}
							· {Math.floor(hit.start_ts / 60)}:{String(Math.floor(hit.start_ts % 60)).padStart(
								2,
								'0',
							)}
						</span>
						<span class="text-sm">{hit.snippet}</span>
					</a>
				{/each}
				{#if searched && hits.length === 0 && !searching}
					<p class="m-0 text-muted-foreground">Nothing in this campaign's transcripts matches.</p>
				{/if}
			</CardContent>
		</Card>

		<Card>
			<CardHeader>
				<CardTitle>Previously on</CardTitle>
				<CardDescription>
					What to read at the table before the next session, from the last three sessions' recaps.
				</CardDescription>
			</CardHeader>
			<CardContent class="flex flex-col gap-2">
				<div>
					<Button
						variant="outline"
						size="sm"
						onclick={() => (previouslyOnOpen = true)}
						disabled={llmProviders.length === 0}
						title={llmProviders.length === 0
							? 'Add an LLM provider (OpenAI-compatible chat) in Settings'
							: ''}
					>
						{previouslyOn ? 'Write it again' : 'Generate'}
					</Button>
				</div>
				{#if previouslyOn}
					<Markdown source={previouslyOn.body} />
				{:else}
					<p class="m-0 text-muted-foreground">
						Nothing written yet. It needs at least one session with a recap or a summary.
					</p>
				{/if}
			</CardContent>
		</Card>

		<Card>
			<CardHeader>
				<CardTitle>Names</CardTitle>
				<CardDescription>
					Every session's extracted names, merged. Tick the ones the next session's transcription
					should be told about.
				</CardDescription>
			</CardHeader>
			<CardContent>
				<ExtractedNames
					groups={nameGroups}
					campaignId={id}
					onadded={() => {
						glossaryToken += 1
					}}
				/>
			</CardContent>
		</Card>

		<Card>
			<CardHeader>
				<CardTitle>Glossary</CardTitle>
				<CardDescription>
					The names and terms this campaign's sessions are transcribed with, in priority order. The
					players above go in ahead of them.
				</CardDescription>
			</CardHeader>
			<CardContent>
				<GlossaryEditor campaignId={id} refreshToken={glossaryToken} rows={8} />
			</CardContent>
		</Card>

		<Card>
			<CardHeader>
				<CardTitle>Settings</CardTitle>
			</CardHeader>
			<CardContent class="flex flex-col gap-3">
				<div class="flex flex-col gap-2">
					<Label for="campaign-name">Name</Label>
					<Input id="campaign-name" bind:value={name} />
				</div>
				<div class="flex flex-col gap-2">
					<Label for="campaign-notes">Notes</Label>
					<Input id="campaign-notes" bind:value={notes} placeholder="Whose table, which system" />
				</div>
				<div class="flex flex-col gap-2">
					<span class="text-sm font-medium">Players</span>
					<!-- A plain list, filled in at the table in one sitting: a row per
					     seat, both names optional, no dialog between the GM and typing
					     the next one. The order is priority order, which is why the
					     arrows are here and why it is said out loud below. -->
					{#each cast as row, i (row)}
						<div class="flex flex-wrap items-center gap-2">
							<Input
								class="min-w-32 flex-1"
								bind:value={row.player}
								placeholder="Player"
								aria-label="Player {i + 1}"
							/>
							<Input
								class="min-w-32 flex-1"
								bind:value={row.character}
								placeholder="Character"
								aria-label="Character {i + 1}"
							/>
							<Button
								variant="ghost"
								size="icon-sm"
								title="Move up"
								aria-label="Move {row.character || row.player || 'this player'} up"
								disabled={i === 0}
								onclick={() => moveCast(i, i - 1)}
							>
								<ArrowUp />
							</Button>
							<Button
								variant="ghost"
								size="icon-sm"
								title="Move down"
								aria-label="Move {row.character || row.player || 'this player'} down"
								disabled={i === cast.length - 1}
								onclick={() => moveCast(i, i + 1)}
							>
								<ArrowDown />
							</Button>
							<Button
								variant="ghost"
								size="icon-sm"
								title="Remove"
								aria-label="Remove {row.character || row.player || 'this player'}"
								onclick={() => cast.splice(i, 1)}
							>
								<Trash2 />
							</Button>
						</div>
					{/each}
					<div>
						<Button
							variant="outline"
							size="sm"
							onclick={() => cast.push({ player: '', character: '' })}
						>
							<Plus />
							Add player
						</Button>
					</div>
					<span class="text-xs text-muted-foreground">
						Sent to the transcription ahead of every glossary term, and told to the model that
						writes the recaps, so it knows which characters are played. Top of the list first: a
						provider that only takes a hundred terms keeps the ones up here.
					</span>
				</div>
				<div class="flex flex-col gap-2">
					<Label for="campaign-recap-prompt">Recap prompt</Label>
					<Textarea
						id="campaign-recap-prompt"
						rows={5}
						bind:value={recapPrompt}
						placeholder="Leave empty to use the default recap instructions from Settings."
					/>
					<span class="text-xs text-muted-foreground">
						Replaces the default recap instructions for this campaign's sessions - another language,
						another voice, another length.
					</span>
				</div>
				<div class="flex flex-wrap items-center gap-2">
					<Button onclick={save} disabled={busy || !name.trim()}>Save</Button>
					<Button variant="destructive" onclick={remove} disabled={busy}>Delete campaign</Button>
					{#if savedMessage}
						<span class="text-sm text-muted-foreground">{savedMessage}</span>
					{/if}
				</div>
			</CardContent>
		</Card>
	</div>
{/if}

<GenerateDialog bind:open={previouslyOnOpen} kind="previously-on" onrun={writePreviouslyOn} />
