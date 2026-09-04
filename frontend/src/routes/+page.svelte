<script lang="ts">
/**
 * The Dashboard: start or stop a capture, and watch the running one.
 *
 * Three concerns that share a screen and nothing else - the capture card owns
 * what a session is started with, and each pane owns its own socket - so the
 * page is only their layout.
 */

import CaptureControls from '$lib/CaptureControls.svelte'
import LiveLogsPane from '$lib/LiveLogsPane.svelte'
import LiveTranscriptPane from '$lib/LiveTranscriptPane.svelte'
</script>

<div class="flex h-[calc(100vh-104px)] flex-col gap-4">
	<CaptureControls />

	<div class="grid min-h-0 flex-1 grid-cols-[3fr_2fr] gap-4">
		<LiveTranscriptPane />
		<LiveLogsPane />
	</div>
</div>
