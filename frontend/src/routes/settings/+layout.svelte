<script lang="ts">
import type { Snippet } from 'svelte'
import { page } from '$app/stores'

let { children }: { children: Snippet } = $props()

const tabs = [
	{ href: '/settings/client', label: 'Client' },
	{ href: '/settings/providers', label: 'Providers' },
	{ href: '/settings/glossary', label: 'Glossary' },
	{ href: '/settings/alerts', label: 'Alerting' },
	{ href: '/settings/services', label: 'Services' },
]
</script>

<h1>Settings</h1>
<nav class="mt-4 mb-6 flex gap-1 border-b">
	{#each tabs as tab (tab.href)}
		<a
			href={tab.href}
			class="border-b-2 px-3 py-2 text-sm {$page.url.pathname === tab.href
        ? 'border-primary font-medium'
        : 'border-transparent text-muted-foreground hover:text-foreground'}"
		>
			{tab.label}
		</a>
	{/each}
</nav>
{@render children()}
