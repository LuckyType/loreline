import { describe, expect, it } from 'vitest'
import { loginUrlWithNext, sanitizeNext } from './loginRedirect'

describe('sanitizeNext', () => {
	it('keeps a same-origin path, query and all', () => {
		expect(sanitizeNext('/sessions/abc?v=2')).toBe('/sessions/abc?v=2')
	})

	it('falls back to the Dashboard for anything a browser would read as a host', () => {
		for (const value of ['//evil.test/', '/\\evil.test/', 'https://evil.test/', 'evil', '']) {
			expect(sanitizeNext(value)).toBe('/')
		}
		expect(sanitizeNext(null)).toBe('/')
		expect(sanitizeNext(undefined)).toBe('/')
	})

	it('never sends a signed-in visitor back to the form', () => {
		expect(sanitizeNext('/login')).toBe('/')
		expect(sanitizeNext('/login?next=/sessions')).toBe('/')
	})
})

describe('loginUrlWithNext', () => {
	it('carries the page that was asked for', () => {
		expect(loginUrlWithNext('/sessions/abc?v=2')).toBe('/login?next=%2Fsessions%2Fabc%3Fv%3D2')
	})

	it('carries nothing when the target is where a bare login lands anyway', () => {
		expect(loginUrlWithNext('/')).toBe('/login')
		expect(loginUrlWithNext('//evil.test/')).toBe('/login')
	})
})
