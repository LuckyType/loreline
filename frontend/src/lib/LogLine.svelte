<script lang="ts">
import { cn } from '$lib/utils'

let { line, wrap = true }: { line: string; wrap?: boolean } = $props()

// Where one key=value pair ends and the next begins. A value is allowed to
// contain spaces, because the ones worth reading do: the very first line of
// every session is
//   audio.capture.start device=Jabra SPEAK 410 USB: Audio (hw:0,0) rate=16000
// and splitting that on whitespace tore the device name - the one thing on the
// line a person actually reads - into five unkeyed grey tokens. So a value
// runs to the next " key=" boundary, or to the end of the line. The key shape
// is the usual identifier one the server writes (device, device_rate), which
// is also what keeps "USB:" and "(hw:0,0)" from being mistaken for a new pair.
const PAIR_BOUNDARY = /\s+(?=[A-Za-z_][A-Za-z0-9_.]*=)/

const parsed = $derived.by(() => {
	const m = line.match(/^(\S+)\s+\[(\w+)\]\s+(\S+)(?:\s+(.*))?$/)
	if (!m) return null
	const extras: { k: string; v: string }[] = []
	const rest = m[4]?.trim()
	if (rest) {
		for (const tok of rest.split(PAIR_BOUNDARY)) {
			const eq = tok.indexOf('=')
			if (eq > 0) extras.push({ k: tok.slice(0, eq), v: tok.slice(eq + 1) })
			else extras.push({ k: '', v: tok })
		}
	}
	return { ts: m[1], level: m[2].toLowerCase(), event: m[3], extras }
})

// Two shades each: the 400s read on a dark ground and wash out on white.
const levelClass: Record<string, string> = {
	debug: 'text-muted-foreground',
	info: 'text-sky-700 dark:text-sky-400',
	warning: 'text-amber-700 dark:text-amber-400',
	warn: 'text-amber-700 dark:text-amber-400',
	error: 'text-red-700 dark:text-red-400',
	critical: 'text-red-700 dark:text-red-500 font-semibold',
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
					><span class="text-emerald-700 dark:text-emerald-400">{extra.v}</span>
				</span>
			{:else}
				<span class="text-muted-foreground">{extra.v}</span>
			{/if}
		{/each}
	</div>
{:else}
	<div class={cn(!wrap && 'whitespace-nowrap')}>{line}</div>
{/if}
