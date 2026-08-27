<script lang="ts">
import { goto } from '$app/navigation'
import { api, ApiError } from '$lib/api'
import { authed } from '$lib/stores'
import { Button } from '$lib/components/ui/button'
import { Input } from '$lib/components/ui/input'
import { Label } from '$lib/components/ui/label'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '$lib/components/ui/card'

let password = $state('')
let error = $state('')
let busy = $state(false)

async function submit(e: Event) {
	e.preventDefault()
	busy = true
	error = ''
	try {
		await api.login(password)
		authed.set(true)
		goto('/')
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'login failed'
	} finally {
		busy = false
	}
}
</script>

<div class="grid min-h-screen place-items-center">
	<Card class="w-80">
		<CardHeader>
			<CardTitle>Loreline</CardTitle>
			<CardDescription>Enter the shared password to continue.</CardDescription>
		</CardHeader>
		<CardContent>
			<form class="flex flex-col gap-4" onsubmit={submit}>
				<div class="flex flex-col gap-2">
					<Label for="pw">Password</Label>
					<Input id="pw" type="password" bind:value={password} autocomplete="current-password" />
				</div>
				{#if error}
					<p class="text-sm text-destructive">{error}</p>
				{/if}
				<Button type="submit" disabled={busy}>
					{busy ? 'Signing in…' : 'Sign in'}
				</Button>
			</form>
		</CardContent>
	</Card>
</div>
