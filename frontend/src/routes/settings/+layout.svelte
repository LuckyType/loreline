<script lang="ts">
import type { Snippet } from 'svelte'
import { page } from '$app/state'

let { children }: { children: Snippet } = $props()

const tabs = [
	{ href: '/settings/client', label: 'Client' },
	{ href: '/settings/providers', label: 'Providers' },
	{ href: '/settings/alerts', label: 'Alerting' },
	{ href: '/settings/services', label: 'Services' },
]
</script>

<!-- No "Settings" heading above the tabs: the left nav already says which page
     this is and highlights it, and it is only ever hidden by someone collapsing
     it on purpose. The tab strip therefore starts at the page padding, which is
     where every settings page's first card would sit anyway.

     The strip can still be wider than a phone, so below sm it scrolls sideways,
     snapping a tab to the left edge, with the scrollbar hidden: the cut-off
     last tab is the cue that there is more. From sm up nothing changes.

     There is no Glossary tab: a glossary belongs to a campaign now, and it is
     edited on the campaign it belongs to. -->
<nav
	class="mb-6 flex gap-1 overflow-x-auto border-b [scrollbar-width:none] sm:overflow-x-visible snap-x [&::-webkit-scrollbar]:hidden"
>
	{#each tabs as tab (tab.href)}
		<a
			href={tab.href}
			class="shrink-0 snap-start border-b-2 px-3 py-2 text-sm {page.url.pathname === tab.href
        ? 'border-primary font-medium'
        : 'border-transparent text-muted-foreground hover:text-foreground'}"
		>
			{tab.label}
		</a>
	{/each}
</nav>
{@render children()}
