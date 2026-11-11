import { sveltekit } from '@sveltejs/kit/vite'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vitest/config'

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	server: {
		proxy: {
			'/api': 'http://127.0.0.1:8000',
			'/ws': { target: 'ws://127.0.0.1:8000', ws: true },
		},
	},
	// Unit tests for the logic that needs no browser: the rules a page applies
	// before it renders anything. They sit beside the module they test.
	test: {
		include: ['src/**/*.test.ts'],
		environment: 'node',
	},
})
