<script lang="ts">
import { confirmState } from '$lib/confirm.svelte'
import { Button } from '$lib/components/ui/button'
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '$lib/components/ui/dialog'
</script>

<Dialog
	bind:open={confirmState.open}
	onOpenChange={(o) => {
		if (!o) confirmState.settle(false)
	}}
>
	<DialogContent class="sm:max-w-sm">
		<DialogHeader>
			<DialogTitle>{confirmState.options.title ?? 'Confirm'}</DialogTitle>
			<DialogDescription>{confirmState.options.description}</DialogDescription>
		</DialogHeader>
		<DialogFooter>
			<Button variant="outline" onclick={() => confirmState.settle(false)}>
				{confirmState.options.cancelLabel ?? 'Cancel'}
			</Button>
			<Button
				variant={confirmState.options.destructive ? 'destructive' : 'default'}
				onclick={() => confirmState.settle(true)}
			>
				{confirmState.options.confirmLabel ?? 'Confirm'}
			</Button>
		</DialogFooter>
	</DialogContent>
</Dialog>
