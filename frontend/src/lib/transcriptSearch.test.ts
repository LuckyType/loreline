import { describe, expect, it } from 'vitest'
import { GAP_SOURCE } from './stores'
import { highlight, matchesQuery } from './transcriptSearch'
import type { TranscriptEvent } from './wire'

function segment(over: Partial<TranscriptEvent>): TranscriptEvent {
	return {
		session_id: 's1',
		source: 'p1',
		text: 'You enter the room.',
		speaker: null,
		start_ts: 0,
		end_ts: 1,
		is_final: true,
		words: [],
		turn_id: null,
		...over,
	} as TranscriptEvent
}

describe('matchesQuery', () => {
	it('matches what was said, whatever the case', () => {
		expect(matchesQuery(segment({}), {}, 'ROOM')).toBe(true)
		expect(matchesQuery(segment({}), {}, 'dragon')).toBe(false)
	})

	it('matches the speaker by label and by display name', () => {
		const event = segment({ speaker: 'Speaker 1' })
		expect(matchesQuery(event, {}, 'speaker 1')).toBe(true)
		expect(matchesQuery(event, { 'Speaker 1': 'Mira' }, 'mira')).toBe(true)
		expect(matchesQuery(event, { 'Speaker 2': 'Mira' }, 'mira')).toBe(false)
	})

	it('treats an empty query as no filter', () => {
		expect(matchesQuery(segment({}), {}, '')).toBe(true)
	})

	it('never hides a gap marker, whatever is searched for', () => {
		expect(matchesQuery(segment({ source: GAP_SOURCE, text: 'lost' }), {}, 'dragon')).toBe(true)
	})
})

describe('highlight', () => {
	it('marks every hit and keeps the text between them', () => {
		expect(highlight('Roll, then roll again', 'roll')).toEqual([
			{ text: 'Roll', hit: true },
			{ text: ', then ', hit: false },
			{ text: 'roll', hit: true },
			{ text: ' again', hit: false },
		])
	})

	it('returns the whole string unhit for an empty query or no match', () => {
		expect(highlight('Roll', '')).toEqual([{ text: 'Roll', hit: false }])
		expect(highlight('Roll', 'x')).toEqual([{ text: 'Roll', hit: false }])
	})
})
