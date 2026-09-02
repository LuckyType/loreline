import type { ModelInfo, ModelPrice } from '$lib/types'

/**
 * Formatting for the model pickers' price/context hints.
 *
 * Only OpenRouter publishes any of this today (see ModelInfo in
 * src/loreline/models.py) - every other provider's entries are a bare id, and
 * `hintFor` returns '' for those so their pickers look exactly as before.
 */

/** `3` -> "$3", `0.6` -> "$0.60", `0.0005` -> "$0.0005".
 *
 *  Sub-$1 prices keep two decimals rather than trimming, so a column of them
 *  lines up ("$0.15 / $0.60", not "$0.15 / $0.6"). Below a cent that flips:
 *  two decimals would collapse the whole cheap end of OpenRouter's catalog
 *  onto an indistinguishable "$0.00", so those keep their digits and trim
 *  trailing zeros instead. */
function usd(value: number): string {
	if (value === 0) return '$0'
	if (value < 0.01) return `$${Number(value.toFixed(4))}`
	if (value < 1) return `$${value.toFixed(2)}`
	return `$${Number(value.toFixed(2))}`
}

/** "$3 / $15" - input then output, per million tokens. A model that costs
 *  nothing either way says so in words: OpenRouter carries a lot of `:free`
 *  variants, and "free" is the thing worth noticing about them. */
export function priceLabel(price: ModelPrice | null): string {
	if (!price) return ''
	if (price.prompt === 0 && price.completion === 0) return 'free'
	const parts = [price.prompt, price.completion].map((v) => (v === null ? '?' : usd(v)))
	return parts.join(' / ')
}

/** `1000000` -> "1M", `128000` -> "128k". */
export function contextLabel(tokens: number | null): string {
	if (!tokens) return ''
	if (tokens >= 1_000_000) return `${Number((tokens / 1_000_000).toFixed(1))}M`
	if (tokens >= 1000) return `${Math.round(tokens / 1000)}k`
	return String(tokens)
}

/** The compact right-hand hint for one option: "$3 / $15 · 1M", or "realtime"
 *  / "batch" for a transcription model where that distinction is what matters.
 *  Empty when the provider published none of it. */
export function hintFor(model: ModelInfo | undefined): string {
	if (!model) return ''
	const parts = [priceLabel(model.pricing), contextLabel(model.context_length)]
	if (model.realtime === true) parts.push('realtime')
	else if (model.realtime === false) parts.push('batch')
	if (model.supports_reasoning) parts.push('reasoning')
	return parts.filter(Boolean).join(' · ')
}

/** Long-form note for a model whose price changes above a prompt length -
 *  shown as a title/tooltip, since a picker row has no space for a ladder.
 *  Empty for the ~87% of models that price one way at any length. */
export function tierNote(model: ModelInfo | undefined): string {
	if (!model?.price_tiers?.length) return ''
	const tiers = model.price_tiers.map(
		(t) => `${priceLabel(t)} above ${contextLabel(t.min_prompt_tokens)} prompt tokens`,
	)
	return `Price per 1M tokens (input / output). Tiered: ${tiers.join('; ')}.`
}

/** Tooltip for any model - the tier ladder when there is one, else a plain
 *  reminder of what the two numbers mean. */
export function priceTitle(model: ModelInfo | undefined): string {
	if (!model?.pricing) return ''
	return tierNote(model) || 'Price per 1M tokens (input / output).'
}
