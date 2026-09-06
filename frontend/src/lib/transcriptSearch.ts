/**
 * Searching a transcript: the same case-insensitive substring match the logs
 * pane filters its feed with, taught to look at a segment rather than a line.
 *
 * The match lives here rather than in the list component because both places
 * that show a transcript filter their own segments before handing them over:
 * each keeps a count in its own header, and a list that quietly dropped rows
 * on its own would leave that count lying. The query comes back down to the
 * list only so the hits can be marked in the text they were found in.
 */

import type { TranscriptEvent } from '$lib/wire'

/** Whether a segment answers the query: matched in what was said, or in who
 *  said it, under the speaker's display name as well as its raw label (the
 *  transcript shows the name, so that is what a reader will search for; the
 *  label is still what an undiarized or unnamed speaker shows). An empty
 *  query matches everything, which is what makes it the "no filter" case. */
export function matchesQuery(
	event: TranscriptEvent,
	names: Record<string, string>,
	query: string,
): boolean {
	const needle = query.toLowerCase()
	if (!needle) return true
	if (event.text.toLowerCase().includes(needle)) return true
	const label = event.speaker
	if (!label) return false
	return label.toLowerCase().includes(needle) || (names[label] ?? '').toLowerCase().includes(needle)
}

/** One run of characters from the searched text, and whether the query hit it. */
export interface TextPart {
	text: string
	hit: boolean
}

/** `text` cut into the runs the query hit and the runs it did not, in order,
 *  so a caller can wrap the hits and leave the rest as it was. Every hit is
 *  marked, not just the first: a segment can say the word twice. An empty
 *  query is a single unhit run, the whole string. */
export function highlight(text: string, query: string): TextPart[] {
	const needle = query.toLowerCase()
	if (!needle) return [{ text, hit: false }]
	const haystack = text.toLowerCase()
	const parts: TextPart[] = []
	let at = 0
	for (let hit = haystack.indexOf(needle, at); hit !== -1; hit = haystack.indexOf(needle, at)) {
		if (hit > at) parts.push({ text: text.slice(at, hit), hit: false })
		parts.push({ text: text.slice(hit, hit + needle.length), hit: true })
		at = hit + needle.length
	}
	if (at < text.length) parts.push({ text: text.slice(at), hit: false })
	return parts
}
