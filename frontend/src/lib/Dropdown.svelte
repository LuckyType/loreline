<script lang="ts">
import { ChevronDown } from '@lucide/svelte'
import { Button } from '$lib/components/ui/button'
import { Input } from '$lib/components/ui/input'
import { Separator } from '$lib/components/ui/separator'
import { portal } from '$lib/portal'
import { cn } from '$lib/utils'

export type DropdownOption = {
	value: string
	label: string
	/** Muted right-hand detail (e.g. a model's price), shown in the list only -
	 *  never on the trigger, which stays a plain readable label. */
	hint?: string
	/** Native tooltip for the row, for detail too long to show inline. */
	title?: string
	/** Greyed out and unpickable, while still visible: an option the selected
	 *  model cannot combine with something already switched on. Say why in
	 *  `title` - a dead row with no explanation is worse than none. */
	disabled?: boolean
}

let {
	value = $bindable(''),
	options = [],
	favorites = [],
	defaultValue = '',
	filterable = false,
	placeholder = 'Select…',
	disabled = false,
	id = undefined,
	class: className = '',
	loading = false,
	onpick = undefined,
	onopen = undefined,
}: {
	value?: string
	options?: DropdownOption[]
	/** Values shown first under a Favorites heading. */
	favorites?: string[]
	/** Value that gets a "default" tag. */
	defaultValue?: string
	filterable?: boolean
	placeholder?: string
	disabled?: boolean
	id?: string
	class?: string
	loading?: boolean
	onpick?: (value: string) => void
	onopen?: () => void
} = $props()

let open = $state(false)
let filter = $state('')
let wrapEl: HTMLDivElement | undefined = $state()
let popEl: HTMLDivElement | undefined = $state()
let triggerEl = $state<HTMLElement | null>(null)
let popStyle = $state('')

// The keyboard's place in the list, held as a *value* rather than an index:
// the rows the list is showing change under it (a model catalogue arrives
// after the popup is already open, the filter box narrows it as you type),
// and a value survives that where a row number does not.
let activeValue = $state('')

const uid = $props.id()
const listId = `${uid}-listbox`
const optionId = (index: number) => `${uid}-option-${index}`

const favSet = $derived(new Set([defaultValue, ...favorites].filter(Boolean)))
const filtered = $derived(
	filter ? options.filter((o) => o.label.toLowerCase().includes(filter.toLowerCase())) : options,
)
const shownFavorites = $derived(filtered.filter((o) => favSet.has(o.value)))
const shownRest = $derived(filtered.filter((o) => !favSet.has(o.value)))
// The rows in the order they are rendered, which is the order the arrow keys
// walk: favourites first, then the rest.
const shown = $derived([...shownFavorites, ...shownRest])
const activeIndex = $derived(activeValue ? shown.findIndex((o) => o.value === activeValue) : -1)
const activeId = $derived(activeIndex >= 0 ? optionId(activeIndex) : undefined)
const selectedLabel = $derived(options.find((o) => o.value === value)?.label ?? value)

// Popover matches the trigger's width, but never shrinks below this - a
// trigger squeezed narrow by a flex sibling (e.g. a ModelPicker with flex-1)
// would otherwise force every option label to wrap.
const MIN_POPOVER_WIDTH_PX = 160

function positionPop() {
	if (!wrapEl) return
	const r = wrapEl.getBoundingClientRect()
	const width = Math.max(r.width, MIN_POPOVER_WIDTH_PX)
	popStyle = `top: ${r.bottom + 4}px; left: ${r.left}px; width: ${width}px;`
}

/** First pickable row at or after `start`, walking in `step`'s direction. */
function enabledFrom(start: number, step: number): number {
	for (let i = start; i >= 0 && i < shown.length; i += step) {
		if (!shown[i].disabled) return i
	}
	return -1
}

function activate(index: number) {
	if (index >= 0) activeValue = shown[index].value
}

function moveActive(step: number) {
	// No wrap-around: the ends of the list are where a native select stops too.
	const from = activeIndex >= 0 ? activeIndex : step > 0 ? -1 : shown.length
	activate(enabledFrom(from + step, step))
}

// Type-ahead, for the lists with no filter box to type into. Keystrokes within
// this window build one prefix; a pause starts a new one.
const TYPE_AHEAD_RESET_MS = 700
let typed = ''
let typedAt = 0

function typeAhead(char: string) {
	const now = Date.now()
	typed = now - typedAt > TYPE_AHEAD_RESET_MS ? char : typed + char
	typedAt = now
	const prefix = typed.toLowerCase()
	activate(shown.findIndex((o) => !o.disabled && o.label.toLowerCase().startsWith(prefix)))
}

function openList() {
	if (disabled || open) return
	filter = ''
	typed = ''
	open = true
	positionPop()
	// Start on what is already selected, so the first ArrowDown steps off the
	// current choice rather than off the top of the list. A selection the list
	// has not loaded yet simply lands nowhere until it does.
	activeValue = value
	onopen?.()
}

/** `refocus` is false only for a click outside: moving focus under a pointer
 *  user who has already clicked elsewhere would yank it back. */
function close(refocus = true) {
	if (!open) return
	open = false
	activeValue = ''
	if (refocus) triggerEl?.focus()
}

function toggle() {
	if (disabled) return
	if (open) close()
	else openList()
}

$effect(() => {
	if (!open) return
	const onMove = () => positionPop()
	window.addEventListener('scroll', onMove, true)
	window.addEventListener('resize', onMove)
	return () => {
		window.removeEventListener('scroll', onMove, true)
		window.removeEventListener('resize', onMove)
	}
})

// The active row is only announced, never focused, so nothing scrolls it into
// the viewport on its own.
$effect(() => {
	if (!open || !activeId) return
	document.getElementById(activeId)?.scrollIntoView({ block: 'nearest' })
})

function pick(v: string) {
	value = v
	close()
	onpick?.(v)
}

function onFilterInput(e: Event) {
	filter = (e.currentTarget as HTMLInputElement).value
	// A narrowed list starts on its first hit, so Enter always has a target.
	activate(enabledFrom(0, 1))
}

function onKeydown(e: KeyboardEvent) {
	if (disabled) return
	if (!open) {
		if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
			e.preventDefault()
			openList()
		}
		return
	}
	if (e.key === 'Escape') {
		// Stopped here: these lists sit inside dialogs, and one Escape should
		// shut the list, not the dialog behind it.
		e.preventDefault()
		e.stopPropagation()
		close()
		return
	}
	if (e.key === 'Tab') {
		// Deliberately not prevented: focus goes back to the trigger and Tab
		// carries on from there, which is where the reader expects to be.
		close()
		return
	}
	if (e.key === 'Enter' || (e.key === ' ' && !filterable)) {
		e.preventDefault()
		const active = shown[activeIndex]
		if (active) pick(active.value)
		return
	}
	if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
		e.preventDefault()
		moveActive(e.key === 'ArrowDown' ? 1 : -1)
		return
	}
	if (e.key === 'Home' || e.key === 'End') {
		e.preventDefault()
		activate(e.key === 'Home' ? enabledFrom(0, 1) : enabledFrom(shown.length - 1, -1))
		return
	}
	if (!filterable && e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
		e.preventDefault()
		typeAhead(e.key)
	}
}

/** Closes on a click anywhere but the popup. Capture phase, so the shield
 *  below has already swallowed the click before any control underneath sees
 *  it. */
function onDocumentClick(e: MouseEvent) {
	if (!open) return
	if (popEl?.contains(e.target as Node)) return
	close(false)
}
</script>

<svelte:document onclickcapture={onDocumentClick} />

<div bind:this={wrapEl} class={className}>
	<Button
		variant="outline"
		{id}
		{disabled}
		bind:ref={triggerEl}
		class="w-full justify-between font-normal"
		onclick={toggle}
		onkeydown={onKeydown}
		type="button"
		role={filterable ? undefined : 'combobox'}
		aria-haspopup="listbox"
		aria-expanded={open}
		aria-controls={open ? listId : undefined}
		aria-activedescendant={open && !filterable ? activeId : undefined}
	>
		<span class={cn('min-w-0 truncate', !value && 'text-muted-foreground')}
			>{selectedLabel || placeholder}</span
		>
		<ChevronDown class="size-3.5 opacity-70" />
	</Button>
	{#if open}
		<div
			class="pointer-events-auto fixed inset-0 z-[60] h-0 overflow-visible"
			data-dropdown-popover
			use:portal={'body'}
		>
			<!-- A shield, not a control: it swallows the click that dismisses the
			     list so the same click does not also hit whatever is underneath.
			     It carries no handler and no role, so unlike the <button> it
			     replaced it never lands in the tab order. onDocumentClick above
			     does the actual closing. -->
			<div class="fixed inset-0 z-[60]" aria-hidden="true"></div>
			<div
				bind:this={popEl}
				class="fixed z-[70] rounded-lg border bg-popover p-1.5 text-sm shadow-xl"
				style={popStyle}
			>
				{#if filterable}
					<Input
						class="mb-1.5"
						placeholder="filter…"
						value={filter}
						oninput={onFilterInput}
						onkeydown={onKeydown}
						autofocus
						role="combobox"
						aria-label="Filter options"
						aria-autocomplete="list"
						aria-expanded="true"
						aria-controls={listId}
						aria-activedescendant={activeId}
					/>
				{/if}
				<div
					id={listId}
					role="listbox"
					aria-label={placeholder}
					class="flex max-h-55 flex-col overflow-y-auto rounded-sm"
				>
					{#snippet row(o: DropdownOption, index: number)}
						<button
							type="button"
							id={optionId(index)}
							role="option"
							tabindex="-1"
							aria-selected={o.value === value}
							aria-disabled={o.disabled ? 'true' : undefined}
							disabled={o.disabled}
							data-active={index === activeIndex}
							class={[
								'flex w-full items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-left hover:bg-accent data-[active=true]:bg-accent disabled:pointer-events-none disabled:opacity-50',
								o.value === value ? 'border border-primary' : 'border border-transparent',
							]}
							onclick={() => pick(o.value)}
							title={o.title}
						>
							<span class="min-w-0 truncate">{o.label}</span>
							<span class="flex shrink-0 items-center gap-1.5">
								{#if o.hint}
									<span class="text-xs whitespace-nowrap text-muted-foreground">{o.hint}</span>
								{/if}
								{#if defaultValue && o.value === defaultValue}
									<span class="rounded-full border px-1.5 text-xs text-muted-foreground"
										>default</span
									>
								{/if}
							</span>
						</button>
					{/snippet}
					{#if shownFavorites.length}
						<!-- `contents` keeps the grouping in the accessibility tree without
						     putting a box in the flex column: a listbox's children are
						     options and groups, not loose headings. -->
						<div class="contents" role="group" aria-label="Favorites">
							<div
								class="px-2 pt-1.5 pb-0.5 text-xs font-medium tracking-widest text-muted-foreground uppercase"
							>
								Favorites
							</div>
							{#each shownFavorites as o, i (o.value)}
								{@render row(o, i)}
							{/each}
						</div>
					{/if}
					{#if shownFavorites.length && shownRest.length}
						<Separator class="my-1.5" />
					{/if}
					{#if shownRest.length}
						<div
							class="contents"
							role={shownFavorites.length ? 'group' : undefined}
							aria-label={shownFavorites.length ? 'All' : undefined}
						>
							{#if shownFavorites.length}
								<div
									class="px-2 pt-1.5 pb-0.5 text-xs font-medium tracking-widest text-muted-foreground uppercase"
								>
									All
								</div>
							{/if}
							{#each shownRest as o, i (o.value)}
								{@render row(o, shownFavorites.length + i)}
							{/each}
						</div>
					{/if}
					{#if !filtered.length}
						<div class="px-2 py-1.5 text-sm text-muted-foreground">
							{loading ? 'Loading…' : 'No options.'}
						</div>
					{/if}
				</div>
			</div>
		</div>
	{/if}
</div>
