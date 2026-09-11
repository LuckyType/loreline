<script lang="ts">
/**
 * The videos generated from a session's summary, in a dialog of their own.
 *
 * They used to sit at the foot of the summary body, which asked far too much
 * of the reader: you had to know they were there, scroll past the whole recap
 * to reach them, and fold the two sections above before a player was tall
 * enough to be worth watching. A dialog is where a video belongs - it opens
 * over the page at a size you can actually watch and closes back to where the
 * reader was, and the trigger in the summary header carries the count, so
 * their existence is visible without opening anything.
 *
 * This is a view and nothing else. The job list, the poll that refreshes it
 * while a generation is in flight, and the delete all belong to the page, so a
 * job that finishes while this is closed is simply there the next time it
 * opens, and one that finishes while it is open appears in place.
 */

import { ChevronDown } from '@lucide/svelte'
import { SvelteSet } from 'svelte/reactivity'
import { Button } from '$lib/components/ui/button'
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '$lib/components/ui/dialog'
import { api } from '$lib/api'
import type { VideoJob } from '$lib/wire'

let {
	open = $bindable(false),
	jobs,
	ondelete,
}: {
	open?: boolean
	/** Every generation this session has, finished or not. Live: the page polls
	 *  it whether or not this dialog is showing. */
	jobs: VideoJob[]
	/** Delete one job and its file. The caller owns the confirmation and the
	 *  refetch, because it owns the list. */
	ondelete?: (jobId: string) => Promise<void> | void
} = $props()

/** What one generation was made with, as one line. Every part is optional
 *  except the model: a model that takes no duration records none, and there is
 *  nothing to say about a length nobody chose. */
function jobMeta(job: VideoJob): string {
	const parts = [job.model]
	if (job.duration) parts.push(`${job.duration}s`)
	if (job.resolution) parts.push(job.resolution)
	return parts.join(' · ')
}

/** Which prompts are open. Collapsed is the default and the list stays
 *  scannable for it: a prompt runs from one line to a whole recap, and a list
 *  of them at full height is a list nobody scrolls to the video in. By id
 *  rather than a flag on the row, because the rows are refetched by the page's
 *  poll while this is open and a flag would be thrown away with them. */
const openPrompts = new SvelteSet<string>()

function togglePrompt(jobId: string) {
	if (!openPrompts.delete(jobId)) openPrompts.add(jobId)
}
</script>

<Dialog bind:open>
	<DialogContent class="sm:max-w-2xl">
		<DialogHeader>
			<DialogTitle>Generated videos</DialogTitle>
			<DialogDescription>
				Made from this session's summary. They are kept on the box, so a generation stays playable
				after the provider's own result link has expired.
			</DialogDescription>
		</DialogHeader>

		<!-- Scrolls inside the dialog rather than growing it: several finished
		     generations are several video elements, and a dialog taller than the
		     window puts its own footer out of reach. -->
		<div class="flex max-h-[65vh] flex-col gap-4 overflow-y-auto">
			{#each jobs as job (job.id)}
				<div class="flex flex-col gap-2">
					<div class="flex flex-wrap items-center justify-between gap-2">
						<span class="min-w-0 truncate text-xs text-muted-foreground" title={jobMeta(job)}>
							{jobMeta(job)}
						</span>
						<span class="flex shrink-0 items-center gap-2">
							{#if job.status === 'queued' || job.status === 'running'}
								<span class="text-xs text-muted-foreground">Generating…</span>
							{:else if job.status === 'error'}
								<span class="text-xs text-destructive">{job.error ?? 'failed'}</span>
							{/if}
							<Button variant="ghost" size="sm" onclick={() => ondelete?.(job.id)}>Delete</Button>
						</span>
					</div>
					<!-- The prompt this was generated from, which was stored from the
					     first version and never shown: a finished video is otherwise
					     four seconds of something with no record of what was asked for,
					     and the one question a second attempt starts from is what the
					     first one said. -->
					<div class="flex flex-col gap-0.5">
						<button
							type="button"
							class="flex w-full items-start gap-1.5 rounded-md text-left text-xs text-muted-foreground"
							aria-expanded={openPrompts.has(job.id)}
							onclick={() => togglePrompt(job.id)}
						>
							<ChevronDown
								class="mt-0.5 size-3.5 shrink-0 transition-transform {openPrompts.has(job.id)
									? ''
									: '-rotate-90'}"
							/>
							<span class="min-w-0 {openPrompts.has(job.id) ? 'whitespace-pre-wrap' : 'truncate'}">
								{job.prompt}
							</span>
						</button>
						{#if job.scene_model}
							<!-- Named rather than implied: a prompt a model condensed out of
							     the recap and one the GM wrote are different things to judge a
							     result against, and only the job knows which this was. -->
							<span class="pl-5 text-xs text-muted-foreground">Scene by {job.scene_model}</span>
						{/if}
					</div>
					{#if job.status === 'done'}
						<!-- svelte-ignore a11y_media_has_caption -->
						<video
							class="w-full rounded-md border"
							controls
							preload="metadata"
							src={api.videoContentUrl(job.id)}
						></video>
					{/if}
				</div>
			{:else}
				<p class="m-0 text-muted-foreground">No videos have been generated for this session.</p>
			{/each}
		</div>

		<DialogFooter>
			<Button variant="outline" onclick={() => (open = false)}>Close</Button>
		</DialogFooter>
	</DialogContent>
</Dialog>
