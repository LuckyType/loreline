/**
 * Seconds since a moment on the wall clock, re-read once a second.
 *
 * The Dashboard's "Recording 12:34" needs a value that moves without anything
 * fetching it: healthz's uptime is the app process's, and polling the session
 * for a number it already knows would be a request per tick. The counting is
 * local, so the only thing worth sharing is the clock, and it stops the moment
 * there is nothing to count from.
 */

/** A live elapsed count from `startedAt` (epoch seconds), `null` while there
 *  is nothing to count from. Call it during a component's setup: it owns the
 *  interval, and that component going away is what clears it. */
export function elapsedSince(startedAt: () => number | null): { readonly seconds: number | null } {
	let nowMs = $state(Date.now())

	$effect(() => {
		if (startedAt() === null) return
		const timer = setInterval(() => {
			nowMs = Date.now()
		}, 1000)
		return () => clearInterval(timer)
	})

	return {
		get seconds() {
			const at = startedAt()
			return at === null ? null : nowMs / 1000 - at
		},
	}
}
