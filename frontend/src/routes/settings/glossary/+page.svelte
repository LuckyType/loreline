<script lang="ts">
/**
 * The always-on default word list, applied to every session whatever campaign
 * it belongs to.
 *
 * The editor itself is shared with the campaign page (GlossaryEditor), which
 * edits one campaign's terms in the same box: the load gating and the clear
 * confirm live there, so both scopes keep them.
 */

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '$lib/components/ui/card'
import GlossaryEditor from '$lib/GlossaryEditor.svelte'
</script>

<Card>
	<CardHeader>
		<CardTitle>Glossary</CardTitle>
		<CardDescription>
			Default word list applied to every session. A campaign's own terms are added to this list,
			never instead of it.
		</CardDescription>
	</CardHeader>
	<CardContent>
		<GlossaryEditor />
	</CardContent>
</Card>
