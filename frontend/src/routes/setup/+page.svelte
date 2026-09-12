<script lang="ts">
/**
 * The first run: claim this instance, then the two things it needs to be
 * useful.
 *
 * Rendered bare, like /login, because a shell with a Logout button and a
 * health dot makes no sense on a box nobody owns yet. The root layout sends
 * anybody here while the instance is unclaimed, and every route but this
 * page's own answers 403 until the claim goes through - see
 * src/loreline/web/first_run.py.
 *
 * Only the first step is compulsory. A provider and a recording route are both
 * skippable and both resumable: the steps are the same ones Settings >
 * Providers and the dashboard's capture card already hold, so a GM who skips
 * everything lands on a dashboard that works and finds them where they live.
 */

import { Check, Cloud, Import, Laptop, Mic, Server } from '@lucide/svelte'
import { onMount } from 'svelte'
import { goto } from '$app/navigation'
import { actionSetup } from '$lib/actionSetup.svelte'
import { ApiError, api } from '$lib/api'
import { clientMic } from '$lib/clientMic.svelte'
import { Badge } from '$lib/components/ui/badge'
import { Button } from '$lib/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '$lib/components/ui/card'
import { Input } from '$lib/components/ui/input'
import { Label } from '$lib/components/ui/label'
import ProviderChoiceList from '$lib/ProviderChoiceList.svelte'
import {
	apiKeyLabel,
	choicesForHosting,
	providerCatalog,
	type ProviderChoice,
} from '$lib/providerCatalog'
import { setup } from '$lib/setup.svelte'
import { authed } from '$lib/stores'
import type { Hosting, InputDevice, ProviderTestResult } from '$lib/wire'

type Step = 'password' | 'provider' | 'capture'

const STEPS: { id: Step; label: string }[] = [
	{ id: 'password', label: 'Password' },
	{ id: 'provider', label: 'Transcription' },
	{ id: 'capture', label: 'Recording' },
]

let step = $state<Step>('password')
let loading = $state(true)

// --- step 1: the claim ------------------------------------------------------

let setupCode = $state('')
let password = $state('')
let passwordConfirm = $state('')
let claiming = $state(false)
let claimError = $state('')

async function claim(e: Event) {
	e.preventDefault()
	claiming = true
	claimError = ''
	try {
		setup.adopt(
			await api.claim({
				setup_code: setupCode,
				password,
				password_confirm: passwordConfirm,
			}),
		)
		// The claim issued the session cookie, so this browser is already
		// signed in: sending it to a login form to retype the password it
		// chose one second ago would be a step that exists only because the
		// code was written in the other order.
		authed.set(true)
		// Nothing on the rest of the wizard needs any of the three again, and
		// the setup code in particular has no business sitting in a form field
		// on a machine that may be shared.
		setupCode = ''
		password = ''
		passwordConfirm = ''
		step = 'provider'
	} catch (err) {
		claimError = err instanceof ApiError ? err.message : 'could not claim this instance'
	} finally {
		claiming = false
	}
}

// --- step 2: a provider -----------------------------------------------------

// The same vendor list Settings > Providers offers, from the same module, so
// the two cannot come to disagree about which vendors exist.
const catalog = $derived(providerCatalog())
let hosting = $state<Hosting | null>(null)
let choice = $state<ProviderChoice | null>(null)
const choices = $derived(hosting ? choicesForHosting(catalog, hosting) : [])

let providerName = $state('')
let baseUrl = $state('')
let apiKey = $state('')
let saving = $state(false)
let providerError = $state('')
/** What the health probe made of the key, once there is a row to probe. The
 *  point of testing here rather than later is that a wrong key found on this
 *  page is one field away from being right; found at the table it is the
 *  evening. */
let probe = $state<ProviderTestResult | null>(null)
let saved = $state(false)
/** What the new row was made the default for, as a phrase, or '' when the
 *  defaults already named somebody. Reported because a silent default is a
 *  setting the GM will meet later without knowing where it came from. */
let seededFor = $state('')

const keyRequired = $derived(choice?.auth === 'api_key')
const canSave = $derived(!!choice && (!keyRequired || !!apiKey.trim()))

function pickProvider(meta: ProviderChoice) {
	choice = meta
	providerName = meta.label
	baseUrl = ''
	apiKey = ''
	probe = null
	saved = false
	seededFor = ''
	providerError = ''
}

function backToVendors() {
	choice = null
	probe = null
	saved = false
	seededFor = ''
	providerError = ''
}

async function saveProvider() {
	if (!choice) return
	saving = true
	providerError = ''
	probe = null
	try {
		const row = await api.createProvider({
			name: providerName.trim() || choice.label,
			kind: choice.kind,
			base_url: baseUrl.trim() || null,
			api_key: apiKey.trim() || null,
			favorite_models: [],
		})
		saved = true
		apiKey = ''
		await actionSetup.reload()
		await seedDefaults(row.id)
		// Asked after the row is stored, because the probe needs a stored key
		// to ask the vendor with. A refusal here is a bad key, not a bad save.
		probe = await api.testProvider(row.id)
		// The state route is what the layout and this page both read; a row now
		// exists, so the answer has changed.
		await setup.refresh()
	} catch (err) {
		providerError = err instanceof ApiError ? err.message : 'could not save this provider'
	} finally {
		saving = false
	}
}

/**
 * Point the stored defaults at the row just added, where nothing points yet.
 *
 * Without this the capture card opens on "no provider selected" the first time
 * it is used, which is a second setup step hiding behind the one just
 * finished. Only ever fills a blank: a default somebody chose on purpose is
 * never overwritten by re-running this wizard, and the gate for what a row can
 * be the default for is the shared one, so a row that cannot transcribe is not
 * offered as the transcription default.
 */
async function seedDefaults(id: string) {
	const defaults = actionSetup.defaults
	const offers = (action: 'transcribe' | 'summarize') =>
		actionSetup.providersFor(action).some((p) => p.id === id)
	const next = { ...defaults }
	if (!next.stt_provider && offers('transcribe')) next.stt_provider = id
	if (!next.summarize_provider && offers('summarize')) next.summarize_provider = id
	const gained: string[] = []
	if (next.stt_provider !== defaults.stt_provider) gained.push('transcription')
	if (next.summarize_provider !== defaults.summarize_provider) gained.push('summaries')
	seededFor = gained.join(' and ')
	if (!gained.length) return
	await actionSetup.saveDefaults(next)
}

// --- step 3: how to record --------------------------------------------------

let devices = $state<InputDevice[]>([])
let devicesChecked = $state(false)

async function loadDevices() {
	try {
		devices = await api.listDevices()
	} catch {
		devices = []
	}
	devicesChecked = true
}

// --- finishing --------------------------------------------------------------

let finishing = $state(false)

async function finish() {
	finishing = true
	try {
		setup.adopt(await api.completeSetup())
	} catch {
		// Not worth blocking the way out of a wizard nobody has to finish: the
		// worst case is being offered it again, which is also what a reader
		// who never got here would see.
	}
	await goto('/')
}

function goToStep(next: Step) {
	step = next
	if (next === 'capture' && !devicesChecked) void loadDevices()
}

onMount(async () => {
	await setup.load()
	// A claimed instance skips the step that claims it, and one that already
	// has a provider row opens on the step after that: the wizard is resumable
	// because it reads where it got to rather than remembering.
	if (setup.unclaimed) step = 'password'
	else if (!setup.providerConfigured) step = 'provider'
	else goToStep('capture')
	if (!setup.unclaimed) void actionSetup.load()
	loading = false
})
</script>

<div class="grid min-h-screen place-items-center p-4">
	<div class="w-full max-w-xl">
		<div class="mb-5 flex items-baseline gap-3">
			<strong class="text-xl">Loreline</strong>
			<span class="text-sm text-muted-foreground">first run</span>
		</div>

		<!-- Where this is going, and how much of it is left. A wizard that shows
		     one card at a time and never says how many there are reads as
		     open-ended, which is what makes people abandon it. -->
		<ol class="mb-4 flex flex-wrap gap-2 text-sm">
			{#each STEPS as s, i (s.id)}
				<li class="flex items-center gap-2">
					<span
						class={[
							'grid size-6 place-items-center rounded-full text-xs',
							s.id === step ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground',
						]}
					>
						{i + 1}
					</span>
					<span class={s.id === step ? 'font-medium' : 'text-muted-foreground'}>{s.label}</span>
				</li>
			{/each}
		</ol>

		{#if loading}
			<Card
				><CardContent class="py-8 text-center text-muted-foreground">Loading…</CardContent></Card
			>
		{:else if step === 'password'}
			<Card>
				<CardHeader>
					<CardTitle>Claim this Loreline</CardTitle>
					<CardDescription>
						Nobody owns this instance yet. Enter the setup code it printed when it started, and
						choose the password everyone at your table will use.
					</CardDescription>
				</CardHeader>
				<CardContent>
					<form class="flex flex-col gap-4" onsubmit={claim}>
						<div class="flex flex-col gap-2">
							<Label for="code">Setup code</Label>
							<Input
								id="code"
								bind:value={setupCode}
								placeholder="ABCD-EFGH"
								autocomplete="off"
								spellcheck={false}
								class="font-mono tracking-widest uppercase"
							/>
							<!-- Naming both commands, because which one applies depends on
							     how it was started and a reader who guesses wrong concludes
							     the code does not exist. -->
							<p class="m-0 text-xs text-muted-foreground">
								It is in this instance's startup log: on the terminal that ran
								<code>docker run</code>, or from <code>docker compose logs app</code> for a stack
								started in the background. It is printed again every time Loreline restarts, until
								it is claimed.
							</p>
						</div>
						<div class="flex flex-col gap-2">
							<Label for="pw">Password</Label>
							<Input id="pw" type="password" bind:value={password} autocomplete="new-password" />
						</div>
						<div class="flex flex-col gap-2">
							<Label for="pw2">Password again</Label>
							<Input
								id="pw2"
								type="password"
								bind:value={passwordConfirm}
								autocomplete="new-password"
							/>
							<!-- The one warning on this page worth reading twice. -->
							<p class="m-0 text-xs text-muted-foreground">
								At least 8 characters. It is stored and never shown again, so a typo here locks this
								instance out: the only way back in is to set
								<code>LORELINE_AUTH_PASSWORD</code>
								on the host and restart, which always wins over the stored one.
							</p>
						</div>
						{#if claimError}
							<p class="m-0 text-sm text-destructive">{claimError}</p>
						{/if}
						<Button type="submit" disabled={claiming}>
							{claiming ? 'Claiming…' : 'Claim and continue'}
						</Button>
					</form>
				</CardContent>
			</Card>
		{:else if step === 'provider'}
			<Card>
				<CardHeader>
					<CardTitle>Who transcribes</CardTitle>
					<CardDescription>
						Loreline sends audio to a speech-to-text vendor and needs one key to do it. You can add
						more, and change any of this, in Settings &gt; Providers later.
					</CardDescription>
				</CardHeader>
				<CardContent class="flex flex-col gap-4">
					{#if saved}
						<div class="flex flex-col gap-3">
							<div class="flex items-center gap-2">
								<Check class="size-4 text-emerald-600" />
								<span
									>{providerName}
									is saved{seededFor ? ` and is now the default for ${seededFor}` : ''}.</span
								>
							</div>
							{#if probe}
								<!-- The probe's own verdict, not a re-reading of it: a rejected
								     key and an unreachable URL are opposite fixes. -->
								<div class="flex flex-wrap items-center gap-2 text-sm">
									<Badge variant={probe.status === 'healthy' ? 'secondary' : 'destructive'}>
										{probe.status}
									</Badge>
									<span class="text-muted-foreground">
										{probe.detail ?? 'The vendor answered and accepted the key.'}
									</span>
								</div>
							{/if}
							<div class="flex gap-2">
								<Button onclick={() => goToStep('capture')}>Continue</Button>
								<Button variant="outline" onclick={backToVendors}>Add another</Button>
							</div>
						</div>
					{:else if choice}
						{@const sel = choice}
						<div class="flex items-center justify-between">
							<strong>{sel.label}</strong>
							<Button variant="outline" size="sm" onclick={backToVendors}>← Back</Button>
						</div>
						<div class="flex flex-col gap-2">
							<Label for="pname">Name</Label>
							<Input id="pname" bind:value={providerName} placeholder={sel.label} />
						</div>
						{#if sel.baseUrlPlaceholder !== null}
							<div class="flex flex-col gap-2">
								<Label for="purl">Base URL</Label>
								<Input id="purl" bind:value={baseUrl} placeholder={sel.baseUrlPlaceholder} />
							</div>
						{/if}
						<div class="flex flex-col gap-2">
							<Label for="pkey">{apiKeyLabel(sel, false)}</Label>
							<Input id="pkey" type="password" bind:value={apiKey} autocomplete="off" />
							{#if sel.keyUrl}
								<p class="m-0 text-xs text-muted-foreground">
									Get one at
									<a
										class="underline underline-offset-2"
										href={sel.keyUrl}
										target="_blank"
										rel="noreferrer"
										>{sel.keyUrl}</a
									>
								</p>
							{/if}
						</div>
						{#if providerError}
							<p class="m-0 text-sm text-destructive">{providerError}</p>
						{/if}
						<div class="flex gap-2">
							<Button onclick={saveProvider} disabled={saving || !canSave}>
								{saving ? 'Saving and testing…' : 'Save and test'}
							</Button>
							<Button variant="ghost" onclick={() => goToStep('capture')}>Skip for now</Button>
						</div>
					{:else if hosting}
						<div class="flex items-center justify-between">
							<span class="text-muted-foreground">
								{hosting === 'cloud' ? 'Cloud providers' : 'Self-hosted providers'}
							</span>
							<Button variant="outline" size="sm" onclick={() => (hosting = null)}>← Back</Button>
						</div>
						<ProviderChoiceList {choices} onpick={pickProvider} />
					{:else}
						{#if setup.providerConfigured}
							<p class="m-0 text-sm text-muted-foreground">
								This instance already has a provider configured. Add another, or carry on.
							</p>
						{/if}
						<div class="flex gap-2">
							<Button onclick={() => (hosting = 'cloud')}>
								<Cloud data-icon="inline-start" />
								Cloud provider
							</Button>
							<Button onclick={() => (hosting = 'selfhosted')}>
								<Server data-icon="inline-start" />
								Self-hosted
							</Button>
						</div>
						<p class="m-0 text-xs text-muted-foreground">
							Cloud needs an API key; self-hosted points at a URL on your network.
						</p>
						<div>
							<Button variant="ghost" onclick={() => goToStep('capture')}>Skip for now</Button>
						</div>
					{/if}
				</CardContent>
			</Card>
		{:else}
			<Card>
				<CardHeader>
					<CardTitle>How you will record</CardTitle>
					<CardDescription>
						Three ways in, and they are not alternatives to choose between now: this is what is
						available on this deployment, so you know which one to reach for at the table.
					</CardDescription>
				</CardHeader>
				<CardContent class="flex flex-col gap-3">
					<div class="rounded-lg border p-3">
						<div class="flex items-center gap-2">
							<Mic class="size-4" />
							<strong>This machine's microphone</strong>
							{#if !devicesChecked}
								<Badge variant="outline">checking…</Badge>
							{:else if devices.length}
								<Badge variant="secondary">{devices.length} available</Badge>
							{:else}
								<Badge variant="outline">none found</Badge>
							{/if}
						</div>
						<p class="m-0 mt-1 text-sm text-muted-foreground">
							{#if devicesChecked && !devices.length}
								This server has no input device Loreline can see, which is normal for a box in a
								cupboard. Use one of the two below instead.
							{:else}
								A sound card on the machine running Loreline. Pick the device in the dashboard's
								capture card before you start a session.
							{/if}
						</p>
					</div>

					<div class="rounded-lg border p-3">
						<div class="flex items-center gap-2">
							<Laptop class="size-4" />
							<strong>This device's microphone</strong>
							{#if clientMic.available}
								<Badge variant="secondary">available</Badge>
							{:else}
								<Badge variant="outline">needs HTTPS</Badge>
							{/if}
						</div>
						<p class="m-0 mt-1 text-sm text-muted-foreground">
							{#if clientMic.available}
								The laptop or phone you are reading this on can be the microphone. Choose "This
								device" in the capture card; the tab then has to stay open for the evening.
							{:else}
								Browsers only hand out a microphone in a secure context, and this page is not one,
								so nothing in Loreline can enable it. Reach it over HTTPS - the README's "Recording
								from a laptop" section sets that up with Tailscale - and this card turns green.
							{/if}
						</p>
					</div>

					<div class="rounded-lg border p-3">
						<div class="flex items-center gap-2">
							<Import class="size-4" />
							<strong>Import a recording</strong>
							<Badge variant="secondary">always available</Badge>
						</div>
						<p class="m-0 mt-1 text-sm text-muted-foreground">
							Record on anything you like and upload the file afterwards. It becomes an ordinary
							session, transcribed on demand, from History &gt; Import.
						</p>
					</div>

					<div class="flex gap-2">
						<Button onclick={finish} disabled={finishing}>
							{finishing ? 'Finishing…' : 'Go to the dashboard'}
						</Button>
						{#if !setup.providerConfigured}
							<Button variant="ghost" onclick={() => goToStep('provider')}>← Back</Button>
						{/if}
					</div>
				</CardContent>
			</Card>
		{/if}
	</div>
</div>
