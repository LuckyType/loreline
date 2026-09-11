/**
 * Which palette the page uses: the operator's pick, kept in this browser.
 *
 * Three values. `dark` and `light` are exactly that; `system` follows the
 * device's own setting through the prefers-color-scheme media query, live, so
 * a phone that switches at sunset takes the page with it. `dark` is the
 * default, since it is what every existing user has had, and a stored value
 * that is none of the three is read as that default rather than trusted.
 *
 * The class on <html> is set from two places, on purpose. app.html carries a
 * script that reads the same key before the first paint and sets the class,
 * so the page never flashes the other palette on load; that script cannot
 * follow the media query afterwards and knows nothing about this store. The
 * root layout's effect on `dark` covers everything after that first paint: a
 * pick on Settings > Client, the device switching palettes, another tab
 * changing the stored value. Anything that changes the key's meaning has to
 * change in both places, which is why the key and the fallback are spelled
 * out there and here.
 */

import { MediaQuery } from 'svelte/reactivity'

export type Theme = 'system' | 'light' | 'dark'

/** The selector's rows, in the order it shows them. */
export const THEME_OPTIONS: { value: Theme; label: string }[] = [
	{ value: 'system', label: 'System' },
	{ value: 'light', label: 'Light' },
	{ value: 'dark', label: 'Dark' },
]

const STORAGE_KEY = 'loreline.theme'
const DEFAULT_THEME: Theme = 'dark'

/** A stored or picked value if it is one of the three, else the default. */
export function parseTheme(value: string | null | undefined): Theme {
	return value === 'light' || value === 'system' ? value : DEFAULT_THEME
}

function readStored(): Theme {
	try {
		return parseTheme(localStorage.getItem(STORAGE_KEY))
	} catch {
		// Storage can be refused (private mode, a locked-down browser); the
		// page is still usable in the default palette.
		return DEFAULT_THEME
	}
}

class ThemeStore {
	#preference = $state<Theme>(readStored())
	#systemDark = new MediaQuery('prefers-color-scheme: dark', true)

	/** The pick itself: what the selector on Settings > Client shows. */
	get preference(): Theme {
		return this.#preference
	}

	set preference(value: Theme) {
		this.#preference = value
		try {
			localStorage.setItem(STORAGE_KEY, value)
		} catch {
			/* best effort, as above */
		}
	}

	/** Whether the page is dark right now: the pick, or the device's answer
	 *  while the pick is `system`. What the <html> class follows. */
	get dark(): boolean {
		if (this.#preference === 'system') return this.#systemDark.current
		return this.#preference === 'dark'
	}

	/** Another tab changed the stored value: adopt it here too, so two tabs do
	 *  not disagree about a setting that claims to be per browser. Wired to
	 *  the window's storage event by the root layout. */
	syncFromStorage(event: StorageEvent): void {
		if (event.key !== STORAGE_KEY) return
		this.#preference = parseTheme(event.newValue)
	}
}

export const theme = new ThemeStore()
