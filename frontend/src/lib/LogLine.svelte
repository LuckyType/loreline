<script lang="ts">
import { cn } from '$lib/utils'

let { line, wrap = true }: { line: string; wrap?: boolean } = $props()

const parsed = $derived.by(() => {
	const m = line.match(/^(\S+)\s+\[(\w+)\]\s+(\S+)(?:\s+(.*))?$/)
	if (!m) return null
	const extras: { k: string; v: string }[] = []
	if (m[4]) {
		for (const tok of m[4].split(/\s+/)) {
			const eq = tok.indexOf('=')
			if (eq > 0) extras.push({ k: tok.slice(0, eq), v: tok.slice(eq + 1) })
			else extras.push({ k: '', v: tok })
		}
	}
	return { ts: m[1], level: m[2].toLowerCase(), event: m[3], extras }
})

const levelClass: Record<string, string> = {
	debug: 'text-muted-foreground',
	info: 'text-sky-400',
	warning: 'text-amber-400',
	warn: 'text-amber-400',
	error: 'text-red-400',
	critical: 'text-red-500 font-semibold',
}
</script>

{#if parsed}
	<div
		class={cn('flex items-baseline gap-x-2', wrap ? 'flex-wrap' : 'flex-nowrap whitespace-nowrap')}
	>
		<span class="shrink-0 text-muted-foreground/70">{parsed.ts}</span>
		<span class={cn('w-16 shrink-0 uppercase', levelClass[parsed.level] ?? 'text-muted-foreground')}
			>{parsed.level}</span
		>
		<span class="text-foreground">{parsed.event}</span>
		{#each parsed.extras as extra, i (i)}
			{#if extra.k}
				<span>
					<span class="text-muted-foreground">{extra.k}=</span
					><span class="text-emerald-400">{extra.v}</span>
				</span>
			{:else}
				<span class="text-muted-foreground">{extra.v}</span>
			{/if}
		{/each}
	</div>
{:else}
	<div class={cn(!wrap && 'whitespace-nowrap')}>{line}</div>
{/if}
