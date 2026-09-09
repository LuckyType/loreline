<script lang="ts">
/**
 * The session's recording, a bar of its own under the card.
 *
 * It brings its own surface rather than sitting in the card's last slot,
 * because it is not part of any one section: whichever of them is open, the
 * playhead is the thing all of them are about. The page hands it the last row
 * of a column sized to the window, so it sits on the bottom edge of the screen
 * without being positioned there - it can no more overlap the card above it
 * than any other block can overlap its sibling.
 *
 * A real <audio> element does the playing - it is what knows how to decode the
 * WAV, and what the browser's own media keys and screen readers already
 * understand - but its native controls are hidden and this bar is built over
 * it instead: one circle to start and stop, the clock at both ends, and a seek
 * bar that doubles as a map of the transcript, with a dot per segment and the
 * one being spoken lit.
 *
 * A session with no recording renders nothing at all, not an empty bar: there
 * is no playhead to dock. An empty one gets the note rather than controls,
 * from the same shared check the export menu labels its entry with: a player
 * that plainly cannot play beats one that looks fine and stays silent.
 */

import { Pause, Play } from '@lucide/svelte'
import { api } from '$lib/api'
import { audioIsEmpty, EMPTY_AUDIO_NOTE, formatTime, GAP_SOURCE } from '$lib/stores'
import { cn } from '$lib/utils'
import type { TranscriptEvent } from '$lib/wire'

let {
	sessionId,
	audioPath,
	audioDurationS,
	segments,
	activeStart = null,
	audioEl = $bindable(null),
	currentTime = $bindable(0),
	onseek,
}: {
	sessionId: string
	/** Set when the session has a stored recording at all. */
	audioPath: string | null | undefined
	/** What the API measured the recording at: the total shown until the
	 *  element has its own metadata, and what decides emptiness. */
	audioDurationS: number | null
	/** The shown transcript, for the dots. Only `start_ts` is read. */
	segments: TranscriptEvent[]
	/** The segment the playhead is in, by its `start_ts`. Its dot is lit. */
	activeStart?: number | null
	/** The element itself, for the page to seek from a transcript timestamp.
	 *  Null whenever there is nothing playable, which is what keeps the page
	 *  from offering to seek a player that is not there. */
	audioEl?: HTMLAudioElement | null
	/** Where the playhead is, for the page to resolve to a segment. */
	currentTime?: number
	/** The playhead was moved by hand rather than running on: the page reads
	 *  it as "show me that segment", even when it is the one already shown. */
	onseek?: () => void
} = $props()

const hasAudio = $derived(!!audioPath)
const empty = $derived(audioIsEmpty(audioDurationS))

// A gap marker is audio nobody transcribed, not a segment - the timeline is a
// map of what was said, so it gets no dot of its own.
const dotSegments = $derived(segments.filter((seg) => seg.source !== GAP_SOURCE))

let duration = $state(0)
let paused = $state(true)

/** How long the recording is: what the element decoded once it had the
 *  metadata, and until then what the API already reported - so the clock on
 *  the right is never a placeholder while a long WAV's header is on the wire. */
const total = $derived(
	Number.isFinite(duration) && duration > 0 ? duration : Math.max(0, audioDurationS ?? 0),
)

/** Where a moment on the session clock sits along the bar, in percent. */
function offset(seconds: number): number {
	if (total <= 0) return 0
	return Math.min(100, Math.max(0, (seconds / total) * 100))
}

function toggle() {
	if (!audioEl) return
	// Started by a click, so autoplay policy allows it; a browser that still
	// declines leaves the player parked where it was, which is no worse than
	// before the click.
	if (audioEl.paused) void audioEl.play().catch(() => {})
	else audioEl.pause()
}
</script>

{#if hasAudio}
	<!-- The card's own surface, spelled out: these are the classes card.svelte
	     and card-content.svelte would have given it, minus the vertical padding
	     a section needs and a control bar does not. `shrink-0` is what makes the
	     card above absorb every pixel this does not take. -->
	<div
		class="shrink-0 rounded-xl bg-card px-6 py-3 text-sm text-card-foreground shadow-xs ring-1 ring-foreground/10"
	>
		{#if empty}
			<p class="m-0 text-muted-foreground">{EMPTY_AUDIO_NOTE}</p>
		{:else}
			<!-- No tint of its own any more: the surface around it already sets the
			     player apart from the card, and a shaded box inside a card-coloured
			     one is two frames drawn for one control. -->
			<div class="flex items-center gap-2.5">
				<!-- preload="metadata" keeps a long session's WAV off the wire until it
				     is played or a timestamp seeks it. -->
				<audio
					bind:this={audioEl}
					bind:currentTime
					bind:duration
					bind:paused
					class="hidden"
					preload="metadata"
					src={api.audioUrl(sessionId)}
				></audio>

				<button
					type="button"
					class="flex size-8 shrink-0 cursor-pointer items-center justify-center rounded-full bg-primary text-primary-foreground hover:bg-primary/85"
					aria-label={paused ? 'Play the recording' : 'Pause the recording'}
					title={paused ? 'Play the recording' : 'Pause the recording'}
					onclick={toggle}
				>
					{#if paused}
						<Play class="size-3.5 translate-x-px fill-current" />
					{:else}
						<Pause class="size-3.5 fill-current" />
					{/if}
				</button>

				<span class="w-9 shrink-0 text-xs tabular-nums text-muted-foreground">
					{formatTime(currentTime)}
				</span>

				<div class="relative flex h-4 min-w-0 flex-1 items-center">
					<div class="absolute inset-x-0 top-1/2 h-1 -translate-y-1/2 rounded-full bg-border">
						<div
							class="h-full rounded-full bg-primary/55"
							style="width: {offset(currentTime)}%"
						></div>
					</div>
					<!-- One dot per segment, keyed by position rather than by start: two
					     speakers can be given the same start and duplicate keys throw. -->
					{#each dotSegments as seg, i (i)}
						<span
							class={cn(
								'pointer-events-none absolute top-1/2 size-[3px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-primary/70',
								seg.start_ts === activeStart && 'size-[5px] bg-foreground',
							)}
							style="left: {offset(seg.start_ts)}%"
						></span>
					{/each}
					<!-- The input covers the whole bar, so a click on a dot lands here
					     and seeks to that dot's moment like any other click would. -->
					<input
						type="range"
						class="absolute inset-0 z-10 m-0 w-full cursor-pointer appearance-none bg-transparent [-webkit-appearance:none] [&::-moz-range-thumb]:size-[11px] [&::-moz-range-thumb]:appearance-none [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:border-0 [&::-moz-range-thumb]:bg-foreground [&::-moz-range-thumb]:shadow-[0_0_0_2px_var(--color-primary)] [&::-moz-range-track]:h-1 [&::-moz-range-track]:bg-transparent [&::-webkit-slider-runnable-track]:h-1 [&::-webkit-slider-runnable-track]:bg-transparent [&::-webkit-slider-thumb]:mt-[-3.5px] [&::-webkit-slider-thumb]:size-[11px] [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-foreground [&::-webkit-slider-thumb]:shadow-[0_0_0_2px_var(--color-primary)] [&::-webkit-slider-thumb]:[-webkit-appearance:none]"
						min="0"
						max={total || 1}
						step="any"
						disabled={total <= 0}
						aria-label="Seek within the recording"
						title="Click or drag to play from a point - the transcript follows"
						bind:value={currentTime}
						oninput={() => onseek?.()}
					>
				</div>

				<span class="w-9 shrink-0 text-right text-xs tabular-nums text-muted-foreground">
					{formatTime(total)}
				</span>
			</div>
		{/if}
	</div>
{/if}
