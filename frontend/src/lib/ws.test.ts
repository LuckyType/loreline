import { describe, expect, it } from 'vitest'
import { backoffDelay, INITIAL_RETRY_DELAY_MS, MAX_RETRY_DELAY_MS } from './ws'

describe('backoffDelay', () => {
	it('doubles the target with each attempt and lands in its top half', () => {
		expect(backoffDelay(0, () => 0)).toBe(INITIAL_RETRY_DELAY_MS / 2)
		expect(backoffDelay(0, () => 1)).toBe(INITIAL_RETRY_DELAY_MS)
		expect(backoffDelay(2, () => 1)).toBe(INITIAL_RETRY_DELAY_MS * 4)
	})

	it('never waits longer than the ceiling', () => {
		expect(backoffDelay(50, () => 1)).toBe(MAX_RETRY_DELAY_MS)
	})

	it('treats a negative attempt as the first', () => {
		expect(backoffDelay(-1, () => 1)).toBe(INITIAL_RETRY_DELAY_MS)
	})
})
