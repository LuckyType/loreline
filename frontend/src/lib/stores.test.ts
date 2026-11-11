import { describe, expect, it } from 'vitest'
import { audioIsEmpty, fmtDuration, formatTime, GAP_SOURCE, sourceLabel, turnKey } from './stores'
import type { ProviderConfig, TranscriptEvent } from './wire'

const providers = [{ id: 'p1', name: 'Deepgram' }] as ProviderConfig[]

describe('durations', () => {
	it('prints minutes and seconds, and hours only once there are any', () => {
		expect(fmtDuration(0, 65)).toBe('1:05')
		expect(fmtDuration(0, 3725)).toBe('1:02:05')
		expect(formatTime(125.9)).toBe('2:05')
	})

	it('prints nothing for a session with no end, and never a negative time', () => {
		expect(fmtDuration(10, null)).toBe('')
		expect(fmtDuration(10, 5)).toBe('0:00')
		expect(formatTime(-3)).toBe('0:00')
	})
})

describe('audioIsEmpty', () => {
	it('calls a bare WAV header empty and an unknown duration not', () => {
		expect(audioIsEmpty(0)).toBe(true)
		expect(audioIsEmpty(1.5)).toBe(false)
		expect(audioIsEmpty(null)).toBe(false)
		expect(audioIsEmpty(undefined)).toBe(false)
	})
})

describe('turnKey', () => {
	const base = { session_id: 's1', source: 'p1' } as TranscriptEvent

	it('is the session, the source and the turn together', () => {
		expect(turnKey({ ...base, turn_id: '0' })).toBe('s1:p1:0')
	})

	it('is absent for a row that belongs to no turn', () => {
		expect(turnKey({ ...base, turn_id: null })).toBeNull()
	})
})

describe('sourceLabel', () => {
	it('names the provider behind a live row', () => {
		expect(sourceLabel('p1', providers)).toBe('Deepgram')
	})

	it('names a gap, a diarization and a re-run for what they are', () => {
		expect(sourceLabel(GAP_SOURCE, providers)).toBe('Lost audio')
		expect(sourceLabel('diarize:original', providers)).toBe('Diarization')
		expect(sourceLabel('reprocess:p1', providers)).toBe('Deepgram (re-run)')
		expect(sourceLabel('reprocess:0123456789abcdef', providers)).toBe('re-run 01234567')
	})
})
