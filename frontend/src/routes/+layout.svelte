<script lang="ts">
import '../app.css'
import { Menu, PanelLeft, X } from '@lucide/svelte'
import type { Snippet } from 'svelte'
import { onDestroy, onMount } from 'svelte'
import { goto } from '$app/navigation'
import { page } from '$app/state'
import { ApiError, api } from '$lib/api'
import { capabilities, loadCapabilities } from '$lib/capabilities.svelte'
import ConfirmDialog from '$lib/ConfirmDialog.svelte'
import { Badge } from '$lib/components/ui/badge'
import { Button } from '$lib/components/ui/button'
import { loginUrlWithNext } from '$lib/loginRedirect'
import { initMagicBento } from '$lib/magicBento'
import { authed, health, logsWs, transcriptWs } from '$lib/stores'
import type { ConnectionStatus } from '$lib/ws'

let { children }: { children: Snippet } = $props()

let timer: ReturnType<typeof setInterval> | null = null

// Sidebar fold state, kept across visits (best effort - private windows etc.).
const NAV_KEY = 'loreline.nav-collapsed'

function loadNavCollapsed(): boolean {
	try {
		return localStorage.getItem(NAV_KEY) === '1'
	} catch {
		return false
	}
}

let navCollapsed = $state(loadNavCollapsed())

function toggleNav() {
	navCollapsed = !navCollapsed
	try {
		localStorage.setItem(NAV_KEY, navCollapsed ? '1' : '0')
	} catch {
		/* best effort */
	}
}

// Below the sm breakpoint the sidebar becomes a dropdown overlay instead of a
// pushed column, so it never eats into a phone's ~390px. Session-only by
// design - unlike navCollapsed there is nothing here worth remembering across
// visits.
let mobileNavOpen = $state(false)
let headerHeight = $state(0)

function closeMobileNav() {
	mobileNavOpen = false
}

// The header's health popover used to open on hover alone, with a keyboard
// fallback written as a variant that does not exist and so never compiled.
// Hover is the wrong interaction here anyway: this app is mostly driven from a
// phone, where there is no hover at all, so the whole diagnostic panel was
// unreachable on its likeliest client. The dot is therefore a disclosure
// button that toggles the panel, dismissed with Escape or a tap outside, and
// the hover behaviour stays for pointers that have one.
let healthOpen = $state(false)
let healthEl: HTMLDivElement | undefined = $state()

function toggleHealth() {
	healthOpen = !healthOpen
	// Opening refetches, which is what clicking the dot always did. While the
	// panel is open the 5s poll keeps it current on its own, so there is
	// nothing left for a second press to refresh.
	if (healthOpen) void poll()
}

function handleWindowKeydown(e: KeyboardEvent) {
	if (e.key !== 'Escape') return
	if (mobileNavOpen) closeMobileNav()
	if (healthOpen) healthOpen = false
}

/** Dismiss on a click anywhere but the popover itself, the same way Dropdown
 *  closes its list. The dot lives inside healthEl, so its own click is left
 *  for the toggle above rather than being closed out from under it. */
function handleDocumentClick(e: MouseEvent) {
	if (!healthOpen) return
	if (healthEl?.contains(e.target as Node)) return
	healthOpen = false
}

const nav = [
	{ href: '/', label: 'Dashboard' },
	{ href: '/sessions', label: 'History' },
	{ href: '/settings', label: 'Settings' },
]

function isActiveNavItem(href: string): boolean {
	return href === '/' ? page.url.pathname === '/' : page.url.pathname.startsWith(href)
}

async function poll() {
	try {
		const h = await api.health()
		health.set(h)
		authed.set(true)
	} catch (err) {
		if (err instanceof ApiError && err.status === 401) {
			authed.set(false)
			// Carry the page being read at the moment the session ran out, so
			// signing back in returns to it rather than to the Dashboard.
			if (page.url.pathname !== '/login') {
				goto(loginUrlWithNext(page.url.pathname + page.url.search))
			}
		}
	}
}

async function logout() {
	await api.logout()
	authed.set(false)
	goto('/login')
}

let magicCleanup: (() => void) | null = null

onMount(() => {
	poll()
	timer = setInterval(poll, 5000)
	// One fetch per page load, shared by every picker. It fails soft: the
	// pickers stay populated and the banner below says the gating is off.
	loadCapabilities()
	magicCleanup = initMagicBento()
})
onDestroy(() => {
	if (timer) clearInterval(timer)
	magicCleanup?.()
})

const healthColor = $derived(
	$health == null ? 'bg-amber-500' : $health.status === 'ok' ? 'bg-emerald-500' : 'bg-red-500',
)

function gib(bytes: number | undefined): string {
	if (!bytes) return '-'
	return `${(bytes / 1024 ** 3).toFixed(1)} GiB`
}

function hms(seconds: number | undefined): string {
	const t = Math.max(0, Math.round(seconds ?? 0))
	const h = Math.floor(t / 3600)
	const m = Math.floor((t % 3600) / 60)
	const s = t % 60
	return `${h}h ${m}m ${s}s`
}

/** A dropped socket that is backing off before its next try is not the same
 *  as a hard offline: amber says "still trying", not "give up". */
function wsDotClass(status: ConnectionStatus): string {
	if (status === 'connected') return 'bg-emerald-500'
	if (status === 'reconnecting') return 'bg-amber-500'
	return 'bg-red-500'
}

function wsLabel(status: ConnectionStatus, liveWord: string): string {
	if (status === 'connected') return liveWord
	if (status === 'reconnecting') return 'reconnecting…'
	return 'offline'
}
</script>

<svelte:window onkeydown={handleWindowKeydown} />
<svelte:document onclickcapture={handleDocumentClick} />

{#if page.url.pathname === '/login'}
	{@render children()}
{:else}
	<div class="flex min-h-screen flex-col">
		<header
			bind:clientHeight={headerHeight}
			class="relative z-30 flex items-center gap-4 border-b bg-card px-5 py-3"
		>
			<Button
				variant="ghost"
				size="icon-sm"
				class="hidden sm:inline-flex"
				onclick={toggleNav}
				aria-label={navCollapsed ? 'Show navigation' : 'Hide navigation'}
			>
				<PanelLeft class="size-4" />
			</Button>
			<Button
				variant="ghost"
				size="icon-sm"
				class="sm:hidden"
				onclick={() => (mobileNavOpen = !mobileNavOpen)}
				aria-label={mobileNavOpen ? 'Close navigation' : 'Open navigation'}
				aria-expanded={mobileNavOpen}
			>
				{#if mobileNavOpen}
					<X class="size-4" />
				{:else}
					<Menu class="size-4" />
				{/if}
			</Button>
			<div class="flex items-baseline gap-2">
				<strong>Loreline</strong>
				<span class="text-muted-foreground">{$health?.version ?? ''}</span>
			</div>
			{#if $health?.capture_status === 'capturing'}
				<Badge variant="secondary" class="gap-2">
					<span class="size-2 rounded-full bg-emerald-500"></span>
					Capturing…
				</Badge>
			{/if}
			<div class="flex-1"></div>
			<div class="group/health relative flex items-center" bind:this={healthEl}>
				<button
					type="button"
					class="flex items-center rounded-md p-2 hover:bg-accent"
					onclick={toggleHealth}
					title="Service health - show details and refresh"
					aria-label="Service health - show details and refresh"
					aria-expanded={healthOpen}
					aria-controls="health-details"
				>
					<span class="size-3 rounded-full {healthColor}"></span>
				</button>
				<div
					id="health-details"
					class="absolute top-full right-0 z-30 mt-1.5 w-60 rounded-lg border bg-popover p-3 text-sm shadow-lg {healthOpen
						? 'visible block'
						: 'invisible hidden group-hover/health:visible group-hover/health:block'}"
				>
					<div class="mt-0 mb-1 text-xs font-medium tracking-wider text-muted-foreground uppercase">
						Client
					</div>
					<div class="flex items-center justify-between gap-6">
						<span class="text-muted-foreground">Service</span
						><strong>{$health?.status ?? '-'}</strong>
					</div>
					<div class="flex items-center justify-between gap-6">
						<span class="text-muted-foreground">Version</span><span>{$health?.version ?? '-'}</span>
					</div>
					<div class="flex items-center justify-between gap-6">
						<span class="text-muted-foreground">Capture</span
						><span>{$health?.capture_status ?? '-'}</span>
					</div>
					<div class="flex items-center justify-between gap-6">
						<span class="text-muted-foreground">Uptime</span
						><span>{hms($health?.uptime_seconds)}</span>
					</div>
					<div class="flex items-center justify-between gap-6">
						<span class="text-muted-foreground">Disk free</span>
						<span>{gib($health?.disk_free_bytes)} / {gib($health?.disk_total_bytes)}</span>
					</div>
					<div class="flex items-center justify-between gap-6">
						<span class="text-muted-foreground">Alerts</span>
						<span>{$health?.alerts_enabled ? 'on' : 'off'}</span>
					</div>

					<div
						class="mt-2.5 mb-1 text-xs font-medium tracking-wider text-muted-foreground uppercase"
					>
						Transcription
					</div>
					<div class="flex items-center justify-between gap-6">
						<span class="text-muted-foreground">Transcript stream</span>
						<span class="flex items-center gap-1.5">
							<span class="size-2 rounded-full {wsDotClass($transcriptWs)}"></span>
							{wsLabel($transcriptWs, 'connected')}
						</span>
					</div>

					<div
						class="mt-2.5 mb-1 text-xs font-medium tracking-wider text-muted-foreground uppercase"
					>
						Logs
					</div>
					<div class="flex items-center justify-between gap-6">
						<span class="text-muted-foreground">Log stream</span>
						<span class="flex items-center gap-1.5">
							<span class="size-2 rounded-full {wsDotClass($logsWs)}"></span>
							{wsLabel($logsWs, 'live')}
						</span>
					</div>
				</div>
			</div>
			<Button variant="outline" size="sm" onclick={logout}>Logout</Button>
		</header>
		{#if mobileNavOpen}
			<div
				class="fixed inset-x-0 bottom-0 z-20 bg-black/50 sm:hidden"
				style="top: {headerHeight}px"
				aria-hidden="true"
				onclick={closeMobileNav}
			></div>
			<nav
				class="fixed inset-x-0 z-20 flex flex-col gap-1 bg-card p-3 shadow-lg sm:hidden"
				style="top: {headerHeight}px"
				aria-label="Navigation"
			>
				{#each nav as item (item.href)}
					<a
						href={item.href}
						class="rounded-lg px-3 py-2 hover:bg-accent {isActiveNavItem(item.href)
              ? 'bg-accent font-medium'
              : ''}"
						onclick={closeMobileNav}
					>
						{item.label}
					</a>
				{/each}
			</nav>
		{/if}
		<div class="grid flex-1 grid-cols-1 {navCollapsed ? '' : 'sm:grid-cols-[200px_1fr]'}">
			{#if !navCollapsed}
				<nav class="hidden flex-col gap-1 border-r bg-card p-3 sm:flex">
					{#each nav as item (item.href)}
						<a
							href={item.href}
							class="rounded-lg px-3 py-2 hover:bg-accent {isActiveNavItem(item.href)
                ? 'bg-accent font-medium'
                : ''}"
						>
							{item.label}
						</a>
					{/each}
				</nav>
			{/if}
			<main class="overflow-auto p-6">
				{#if capabilities.error}
					<div
						class="mb-4 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-600"
					>
						<span>{capabilities.error}</span>
						<button class="underline underline-offset-2" onclick={() => capabilities.reload()}>
							Retry
						</button>
					</div>
				{/if}
				{@render children()}
			</main>
		</div>
	</div>
{/if}

<ConfirmDialog />
