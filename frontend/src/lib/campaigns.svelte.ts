/**
 * The campaign list, once per browser session, shared.
 *
 * Four places need the same list for the same reason: to turn a session's
 * `campaign_id` into a name, or to offer the names as a choice. The capture
 * card, the History table and its filter, and the session header all used to
 * be candidates for fetching it themselves, which is three requests for one
 * list and three chances to disagree about what is in it after a rename.
 *
 * Deduped and reloadable like `actionSetup`, and for the same reason: a page
 * that creates or renames a campaign calls `reload()` so every picker on the
 * screen agrees within one tick, rather than until the next navigation.
 */

import { api } from './api'
import type { CampaignSummary } from './wire'

class CampaignStore {
	/** Empty until the first successful fetch. Replaced whole, never mutated. */
	rows = $state.raw<CampaignSummary[]>([])
	/** Non-empty when the list could not be fetched. */
	error = $state('')
	/** True once the first fetch has settled, success or failure. Blank rows and
	 *  a blank error look identical to a fresh install with no campaigns, and
	 *  this is the only field that tells the two apart - a picker reads false as
	 *  "still loading" rather than as "nothing to offer". */
	ready = $state(false)
	#loaded = false
	#inflight: Promise<void> | null = null

	/** Fetch once and share the result. Resolves either way: a failure is
	 *  recorded in `error`, never thrown, because every caller here is drawing a
	 *  picker beside something more important than the picker. */
	load(): Promise<void> {
		if (this.#loaded) return Promise.resolve()
		if (this.#inflight) return this.#inflight
		this.#inflight = this.#fetch().finally(() => {
			this.#inflight = null
		})
		return this.#inflight
	}

	/** Re-fetch after a page added, renamed or deleted one. The current rows
	 *  stay until the new ones land, so nothing seeded from them flickers. */
	reload(): Promise<void> {
		this.#loaded = false
		return this.load()
	}

	async #fetch(): Promise<void> {
		try {
			this.rows = await api.listCampaigns()
			this.error = ''
			this.#loaded = true
		} catch (err) {
			this.error = err instanceof Error ? err.message : 'failed to load campaigns'
		}
		this.ready = true
	}

	/** Create one and adopt the new list, answering with the row created.
	 *  Throws on failure: every caller here has a message slot for it. */
	async create(name: string) {
		const campaign = await api.createCampaign({ name })
		await this.reload()
		return campaign
	}

	/** The campaign's name, or '' for a session that is in none. An id nothing
	 *  answers to also reads as '': that is a campaign deleted out from under a
	 *  row, and the honest thing to show is the same as no campaign rather than
	 *  a hex id nobody can act on. */
	name(id: string | null | undefined): string {
		if (!id) return ''
		return this.rows.find((row) => row.campaign.id === id)?.campaign.name ?? ''
	}

	/** The rows as a Dropdown would take them, with the campaign-less choice
	 *  first. Said once, because the capture card, the History filter and the
	 *  session header's Change dialog all offer exactly this list. */
	options(noneLabel = 'No campaign') {
		return [
			{ value: '', label: noneLabel },
			...this.rows.map((row) => ({
				value: row.campaign.id,
				label: row.campaign.name,
			})),
		]
	}
}

export const campaigns = new CampaignStore()
