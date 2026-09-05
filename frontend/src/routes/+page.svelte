<script lang="ts">
/**
 * The Dashboard: start or stop a capture, and watch the running one.
 *
 * Three concerns that share a screen and nothing else - the capture card owns
 * what a session is started with, and each pane owns its own socket - so the
 * page is only their layout.
 *
 * That layout is a dock: the Transcript is the primary pane, so it takes the
 * full width and whatever height is left over, and the Logs pane sits under it
 * at a height the user drags. The pane owns its own height; the page owns only
 * the ceiling, since only the page knows how much room the Transcript needs.
 */

import CaptureControls from '$lib/CaptureControls.svelte'
import LiveLogsPane from '$lib/LiveLogsPane.svelte'
import LiveTranscriptPane from '$lib/LiveTranscriptPane.svelte'

/** What the Transcript keeps for itself, the gap between the cards included:
 *  its own title bar plus a few lines. Logs never grows past it. */
const MIN_TRANSCRIPT_HEIGHT = 180

let dockHeight = $state(0)

/** The tallest Logs may be dragged. Zero until the dock has been measured,
 *  which the pane reads as "no ceiling yet" rather than "no room at all". */
const maxLogsHeight = $derived(dockHeight > 0 ? Math.max(1, dockHeight - MIN_TRANSCRIPT_HEIGHT) : 0)
</script>

<div class="flex h-[calc(100vh-104px)] flex-col gap-4">
	<CaptureControls />

	<div class="flex min-h-0 flex-1 flex-col gap-4" bind:clientHeight={dockHeight}>
		<LiveTranscriptPane />
		<LiveLogsPane maxHeight={maxLogsHeight} />
	</div>
</div>
