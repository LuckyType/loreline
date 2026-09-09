/**
 * Carrying "where were you going?" through the login form.
 *
 * Two places bounce a visitor whose session has gone: the 401 handler in
 * api.ts and the layout's health poll. Both of them know the page that was
 * actually being asked for, and both used to throw it away, so a deep link to
 * a session always finished on the Dashboard and the reader had to find their
 * way back by hand. The destination travels as a query parameter instead, and
 * the login page finishes the journey.
 */

const PARAM = 'next'

/** The login URL to bounce to, carrying `target` when it is worth carrying.
 *  The Dashboard is where a bare /login lands anyway, and /login itself would
 *  only send the visitor back to the form they just filled in. */
export function loginUrlWithNext(target: string): string {
	const safe = sanitizeNext(target)
	return safe === '/' ? '/login' : `/login?${PARAM}=${encodeURIComponent(safe)}`
}

/** Where to go once signed in, given whatever the query parameter holds.
 *
 * Only a same-origin path is accepted, and the guard is deliberately about the
 * shape of the string rather than about parsing it: a browser reads both
 * "//evil.test/" and "/\evil.test/" as a host rather than as a path, so a
 * single leading slash is the whole rule. Anything else - an absolute URL, a
 * bare word, a missing parameter - falls back to the Dashboard, which makes
 * this form useless as an open redirect. */
export function sanitizeNext(value: string | null | undefined): string {
	if (!value) return '/'
	if (!value.startsWith('/')) return '/'
	if (value.startsWith('//') || value.startsWith('/\\')) return '/'
	if (value === '/login' || value.startsWith('/login?')) return '/'
	return value
}
