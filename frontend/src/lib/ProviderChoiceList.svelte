<script lang="ts">
/**
 * The list of vendors to pick from, rendered the same way wherever it is asked.
 *
 * Shared by Settings > Providers and the first-run wizard so the two cannot
 * end up offering different vendors, in a different order, with different
 * descriptions. The data behind it is $lib/providerCatalog.
 */
import { Button } from '$lib/components/ui/button'
import type { ProviderChoice } from '$lib/providerCatalog'

let { choices, onpick }: { choices: ProviderChoice[]; onpick: (choice: ProviderChoice) => void } =
	$props()
</script>

<div class="mt-2 flex flex-col gap-1.5">
	{#each choices as meta (meta.kind)}
		<Button
			variant="outline"
			class="h-auto flex-col items-start gap-0.5 py-2"
			onclick={() => onpick(meta)}
		>
			<strong>{meta.label}</strong>
			<span class="text-xs font-normal text-muted-foreground">{meta.note}</span>
		</Button>
	{/each}
</div>
