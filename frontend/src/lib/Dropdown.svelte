<script lang="ts">
import { ChevronDown } from '@lucide/svelte'
import { Button } from '$lib/components/ui/button'
import { Input } from '$lib/components/ui/input'
import { Separator } from '$lib/components/ui/separator'
import { portal } from '$lib/portal'
import { cn } from '$lib/utils'

export type DropdownOption = { value: string; label: string }

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
let popStyle = $state('')

const favSet = $derived(new Set([defaultValue, ...favorites].filter(Boolean)))
const filtered = $derived(
	filter ? options.filter((o) => o.label.toLowerCase().includes(filter.toLowerCase())) : options,
)
const shownFavorites = $derived(filtered.filter((o) => favSet.has(o.value)))
const shownRest = $derived(filtered.filter((o) => !favSet.has(o.value)))
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

function toggle() {
	if (disabled) return
	open = !open
	if (open) {
		filter = ''
		positionPop()
		onopen?.()
	}
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

function pick(v: string) {
	value = v
	open = false
	onpick?.(v)
}
</script>

<div bind:this={wrapEl} class={className}>
	<Button
		variant="outline"
		{id}
		{disabled}
		class="w-full justify-between font-normal"
		onclick={toggle}
		type="button"
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
			<button
				type="button"
				class="fixed inset-0 z-[60] h-full w-full border-0 bg-transparent p-0"
				aria-label="Close list"
				onclick={() => (open = false)}
			></button>
			<div
				class="fixed z-[70] rounded-lg border bg-popover p-1.5 text-sm shadow-xl"
				style={popStyle}
			>
				{#if filterable}
					<Input
						class="mb-1.5"
						placeholder="filter…"
						bind:value={filter}
						autofocus
						onkeydown={(e) => e.key === 'Escape' && (open = false)}
					/>
				{/if}
				<div class="flex max-h-55 flex-col overflow-y-auto rounded-sm">
					{#if shownFavorites.length}
						<div
							class="px-2 pt-1.5 pb-0.5 text-xs font-medium tracking-widest text-muted-foreground uppercase"
						>
							Favorites
						</div>
					{/if}
					{#each shownFavorites as o (o.value)}
						<button
							type="button"
							class="flex w-full items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-left hover:bg-accent {o.value ===
              value
                ? 'border border-primary'
                : 'border border-transparent'}"
							onclick={() => pick(o.value)}
						>
							<span>{o.label}</span>
							{#if defaultValue && o.value === defaultValue}
								<span class="rounded-full border px-1.5 text-xs text-muted-foreground"
									>default</span
								>
							{/if}
						</button>
					{/each}
					{#if shownFavorites.length && shownRest.length}
						<Separator class="my-1.5" />
					{/if}
					{#if shownRest.length && shownFavorites.length}
						<div
							class="px-2 pt-1.5 pb-0.5 text-xs font-medium tracking-widest text-muted-foreground uppercase"
						>
							All
						</div>
					{/if}
					{#each shownRest as o (o.value)}
						<button
							type="button"
							class="flex w-full items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-left hover:bg-accent {o.value ===
              value
                ? 'border border-primary'
                : 'border border-transparent'}"
							onclick={() => pick(o.value)}
						>
							<span>{o.label}</span>
							{#if defaultValue && o.value === defaultValue}
								<span class="rounded-full border px-1.5 text-xs text-muted-foreground"
									>default</span
								>
							{/if}
						</button>
					{/each}
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
