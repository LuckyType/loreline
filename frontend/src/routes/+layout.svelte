<script lang="ts">
import '../app.css'
import { PanelLeft } from '@lucide/svelte'
import type { Snippet } from 'svelte'
import { onDestroy, onMount } from 'svelte'
import { goto } from '$app/navigation'
import { page } from '$app/state'
import { ApiError, api } from '$lib/api'
import { capabilities, loadCapabilities } from '$lib/capabilities.svelte'
import ConfirmDialog from '$lib/ConfirmDialog.svelte'
import { Badge } from '$lib/components/ui/badge'
import { Button } from '$lib/components/ui/button'
import { initMagicBento } from '$lib/magicBento'
import { authed, health, logsWs, transcriptWs } from '$lib/stores'

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
			if (page.url.pathname !== '/login') goto('/login')
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
</script>

{#if page.url.pathname === '/login'}
	{@render children()}
{:else}
	<div class="flex min-h-screen flex-col">
		<header class="flex items-center gap-4 border-b bg-card px-5 py-3">
			<Button
				variant="ghost"
				size="icon-sm"
				onclick={toggleNav}
				aria-label={navCollapsed ? 'Show navigation' : 'Hide navigation'}
			>
				<PanelLeft class="size-4" />
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
			<div class="group/health relative flex items-center">
				<button
					class="flex items-center rounded-md p-2 hover:bg-accent"
					onclick={poll}
					title="Service health - click to refresh"
					aria-label="Service health - click to refresh"
				>
					<span class="size-3 rounded-full {healthColor}"></span>
				</button>
				<div
					class="invisible absolute top-full right-0 z-30 mt-1.5 hidden w-60 rounded-lg border bg-popover p-3 text-sm shadow-lg group-hover/health:visible group-hover/health:block focus-within/health:visible focus-within/health:block"
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
							<span
								class="size-2 rounded-full {$transcriptWs ? 'bg-emerald-500' : 'bg-red-500'}"
							></span>{$transcriptWs ? 'connected' : 'offline'}
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
							<span class="size-2 rounded-full {$logsWs ? 'bg-emerald-500' : 'bg-red-500'}"></span>
							{$logsWs ? 'live' : 'offline'}
						</span>
					</div>
				</div>
			</div>
			<Button variant="outline" size="sm" onclick={logout}>Logout</Button>
		</header>
		<div class="grid flex-1 {navCollapsed ? 'grid-cols-1' : 'grid-cols-[200px_1fr]'}">
			{#if !navCollapsed}
				<nav class="flex flex-col gap-1 border-r bg-card p-3">
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
