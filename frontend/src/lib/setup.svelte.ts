/**
 * Which of the three states this instance is in, for the whole browser session.
 *
 * One store because two readers need the same answer and must not each fetch
 * it: the root layout, which sends a visitor to the wizard when the instance
 * has not been claimed, and the wizard itself, which decides from it which
 * step to open on. The claim and the finish both hand their fresh answer back
 * here (`adopt`) rather than triggering another round trip, so the layout
 * stops redirecting in the same tick the claim succeeds.
 *
 * `GET /api/setup/state` is the one route an unclaimed instance answers, so
 * this is also the only fetch that can succeed before anybody has signed in.
 */

import { api } from '$lib/api'
import type { SetupState, SetupStateKind } from '$lib/wire'

class SetupStore {
	/** Null until the first fetch settles. Nothing may act on the answer
	 *  before then: treating "not loaded" as "claimed" would let a page fire a
	 *  screenful of 403s, and treating it as "unclaimed" would bounce a
	 *  perfectly signed-in reader to the wizard. Read `ready` instead. */
	state = $state<SetupStateKind | null>(null)
	providerConfigured = $state(false)
	/** Whether somebody has finished or skipped through the wizard. Says
	 *  nothing about whether the steps were actually done: every step after
	 *  the claim is optional on purpose. */
	wizardComplete = $state(false)
	/** True once the first fetch has settled, success or failure. */
	ready = $state(false)
	/** Non-empty when the state could not be read at all. Fail-soft: an
	 *  instance whose setup route is unreachable is not one to lock a reader
	 *  out of, so the layout carries on as if it were claimed. */
	error = $state('')

	#loaded = false
	#inflight: Promise<void> | null = null

	/** Fetch once per page load and share the result. Resolves either way. */
	load(): Promise<void> {
		if (this.#loaded) return Promise.resolve()
		if (this.#inflight) return this.#inflight
		this.#inflight = this.#fetch().finally(() => {
			this.#inflight = null
		})
		return this.#inflight
	}

	/** Re-read after something changed the answer from outside this store. */
	refresh(): Promise<void> {
		this.#loaded = false
		return this.load()
	}

	/** Take the state a setup call answered with, instead of fetching it again. */
	adopt(next: SetupState): void {
		this.state = next.state
		this.providerConfigured = next.provider_configured
		this.wizardComplete = next.wizard_complete
		this.error = ''
		this.#loaded = true
		this.ready = true
	}

	async #fetch(): Promise<void> {
		try {
			this.adopt(await api.setupState())
		} catch (err) {
			this.error = err instanceof Error ? err.message : 'could not read the setup state'
			this.ready = true
		}
	}

	/** Nobody owns this instance yet: every other route answers 403 and the
	 *  visitor belongs in the wizard, wherever they were heading. */
	get unclaimed(): boolean {
		return this.state === 'unclaimed'
	}

	/** There is still a wizard worth offering. False while it is unknown, so
	 *  nothing flashes an invitation that a loaded state would withdraw. */
	get unfinished(): boolean {
		return this.ready && this.state !== null && (this.unclaimed || !this.wizardComplete)
	}
}

export const setup = new SetupStore()
