<script lang="ts" module>
/**
 * The small slice of markdown a model actually writes, rendered.
 *
 * A session summary comes back from a chat model, and every current one answers
 * "write a summary" in markdown whether or not it was asked to. Rendered as
 * plain text the reader saw literal `##`, `###` and `**`, which is worst
 * exactly where it matters: the headings are what make an eight-hour recap
 * skimmable.
 *
 * Two decisions shape this file.
 *
 * 1. No HTML is ever produced, and so none is ever inserted. The source is
 *    parsed into spans and blocks and rendered through Svelte's own text
 *    interpolation, which escapes. There is no `{@html}` here and there must
 *    not be one: the text comes from a model, over the wire, and "sanitise the
 *    output" is a promise that can be broken by a later edit, while "never
 *    build a string of HTML" cannot. That also keeps a markdown parser and a
 *    sanitiser out of package.json for one paragraph of recap.
 * 2. The subset is deliberately narrow: ATX headings, bullet and numbered
 *    lists, paragraphs, `**bold**`, `*italic*` and `` `code` ``. Anything else
 *    falls through as the literal text it already was, which is no worse than
 *    today's rendering. Links are left out with the rest: a recap of a spoken
 *    session has no URLs in it, and not building an `href` at all is one less
 *    thing to get wrong. Underscore emphasis is left out on purpose too -
 *    diarized transcripts are full of SPEAKER_00 and SPEAKER_01, and treating
 *    `_` as emphasis would italicise the middle of every speaker label.
 */

/** One run of text in a line, with whatever emphasis it was written under. */
export type Span = { text: string; bold?: boolean; italic?: boolean; code?: boolean }

/** One block of the document. Lists collect their own items so a run of `-`
 *  lines becomes one `<ul>` rather than a stack of one-item lists. */
export type Block =
	| { kind: 'heading'; level: number; spans: Span[] }
	| { kind: 'paragraph'; spans: Span[] }
	| { kind: 'list'; ordered: boolean; items: Span[][] }

// `**bold**` before `*italic*` so the greedier delimiter wins, and backticks
// first so emphasis markers inside code stay literal. Both emphasis forms
// require the delimiters to hug non-space, the same rule CommonMark uses and
// the reason `5 * 3 * 2` stays arithmetic instead of turning into italics.
const INLINE = /`([^`]+)`|\*\*(\S(?:[^*]*\S)?)\*\*|\*(\S(?:[^*\n]*\S)?)\*/g

/** Split one line into its emphasised and plain runs. */
export function spansOf(line: string): Span[] {
	const spans: Span[] = []
	let at = 0
	for (const m of line.matchAll(INLINE)) {
		if (m.index > at) spans.push({ text: line.slice(at, m.index) })
		if (m[1] !== undefined) spans.push({ text: m[1], code: true })
		else if (m[2] !== undefined) spans.push({ text: m[2], bold: true })
		else spans.push({ text: m[3] ?? '', italic: true })
		at = m.index + m[0].length
	}
	if (at < line.length) spans.push({ text: line.slice(at) })
	return spans
}

const HEADING = /^(#{1,6})\s+(.+)$/
const BULLET = /^\s*[-*+]\s+(.+)$/
const NUMBERED = /^\s*\d+[.)]\s+(.+)$/

/** Parse the document into blocks. Line-based on purpose: a recap is prose,
 *  headings and bullets, and a line is enough to tell those apart. */
export function blocksOf(source: string): Block[] {
	const blocks: Block[] = []
	let paragraph: string[] = []

	// Consecutive non-blank prose lines are one paragraph, joined by the soft
	// breaks they were written with - the old renderer kept those with
	// `whitespace-pre-wrap`, and losing them would reflow a model's line breaks
	// into a wall.
	function flush() {
		if (paragraph.length) blocks.push({ kind: 'paragraph', spans: spansOf(paragraph.join('\n')) })
		paragraph = []
	}

	for (const raw of source.split('\n')) {
		const line = raw.trimEnd()
		if (!line.trim()) {
			flush()
			continue
		}
		const heading = HEADING.exec(line)
		if (heading) {
			flush()
			blocks.push({ kind: 'heading', level: heading[1].length, spans: spansOf(heading[2]) })
			continue
		}
		const bullet = BULLET.exec(line)
		const numbered = bullet ? null : NUMBERED.exec(line)
		if (bullet || numbered) {
			flush()
			const ordered = !bullet
			const item = spansOf((bullet ?? numbered)?.[1] ?? '')
			const last = blocks[blocks.length - 1]
			// Append to the run above when it is a list of the same kind, so the
			// numbering and the bullets each stay one list.
			if (last?.kind === 'list' && last.ordered === ordered) last.items.push(item)
			else blocks.push({ kind: 'list', ordered, items: [item] })
			continue
		}
		paragraph.push(line)
	}
	flush()
	return blocks
}

/**
 * How a summary's heading is sized, and what element it becomes.
 *
 * The card around this already owns an `<h1>` ("Session") and an `<h3>` (the
 * "Summary" fold header), so a summary's own headings start below both: they
 * are content inside a section, not peers of it. The element and the styling
 * are decided separately - the level keeps the document outline honest for a
 * screen reader, the classes keep a model's `##` from out-shouting the page's
 * real headings, which is what "text-sm" and a weight step are for.
 */
const HEADING_STYLES: Record<number, { tag: 'h4' | 'h5' | 'h6'; class: string }> = {
	1: { tag: 'h4', class: 'text-base font-semibold' },
	2: { tag: 'h5', class: 'text-sm font-semibold' },
	3: { tag: 'h6', class: 'text-sm font-semibold text-muted-foreground' },
}
const DEEP_HEADING = {
	tag: 'h6',
	class: 'text-xs font-semibold tracking-wide text-muted-foreground uppercase',
} as const

function headingStyle(level: number) {
	return HEADING_STYLES[level] ?? DEEP_HEADING
}
</script>

<script lang="ts">
import { cn } from '$lib/utils'

let {
	source,
	class: className,
}: {
	/** The markdown to render. Trusted for its content, never for its markup. */
	source: string
	class?: string
} = $props()

const blocks = $derived(blocksOf(source))
</script>

{#snippet inline(spans: Span[])}
	{#each spans as span, i (i)}
		{#if span.code}
			<code class="rounded bg-accent/60 px-1 py-0.5 text-[0.9em]">{span.text}</code>
		{:else if span.bold}
			<strong class="font-semibold">{span.text}</strong>
		{:else if span.italic}
			<em>{span.text}</em>
		{:else}
			{span.text}
		{/if}
	{/each}
{/snippet}

<div class={cn('flex flex-col gap-2 leading-relaxed', className)}>
	{#each blocks as block, i (i)}
		{#if block.kind === 'heading'}
			{@const style = headingStyle(block.level)}
			<!-- Not the first block: a heading opening the recap needs no gap above
			     it, one following a paragraph does. -->
			<svelte:element this={style.tag} class={cn('m-0', style.class, i > 0 && 'mt-2')}>
				{@render inline(block.spans)}
			</svelte:element>
		{:else if block.kind === 'list'}
			{#if block.ordered}
				<ol class="m-0 flex list-decimal flex-col gap-1 pl-5">
					{#each block.items as item, j (j)}
						<li>{@render inline(item)}</li>
					{/each}
				</ol>
			{:else}
				<ul class="m-0 flex list-disc flex-col gap-1 pl-5">
					{#each block.items as item, j (j)}
						<li>{@render inline(item)}</li>
					{/each}
				</ul>
			{/if}
		{:else}
			<p class="m-0 whitespace-pre-wrap">{@render inline(block.spans)}</p>
		{/if}
	{/each}
</div>
