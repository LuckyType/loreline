import js from '@eslint/js'
import svelte from 'eslint-plugin-svelte'
import globals from 'globals'
import ts from 'typescript-eslint'
import prettier from 'eslint-config-prettier'

export default ts.config(
	js.configs.recommended,
	...ts.configs.recommended,
	...svelte.configs['flat/recommended'],
	prettier,
	...svelte.configs['flat/prettier'],
	{
		languageOptions: {
			globals: { ...globals.browser, ...globals.node },
		},
	},
	{
		files: ['**/*.svelte'],
		languageOptions: {
			parserOptions: { parser: ts.parser },
		},
	},
	{
		// CLI-generated shadcn-svelte components; $props() rest-element warnings don't apply
		// (never used as custom elements).
		files: ['src/lib/components/ui/**/*.svelte'],
		rules: {
			'svelte/valid-compile': 'off',
			'svelte/no-unused-svelte-ignore': 'off',
		},
	},
	{
		// Svelte 5 `generics="T = never"` components (bits-ui Tooltip wrappers):
		// eslint-plugin-svelte's parser doesn't bind the generic as a type, so
		// every use of `T` in the props type reads as an undefined reference.
		files: ['src/lib/components/ui/tooltip/*.svelte'],
		rules: {
			'no-undef': 'off',
		},
	},
	{
		// The API document and the wire types generated from it: not written
		// here, not formatted here (see `npm run gen:api`).
		ignores: ['build/', '.svelte-kit/', 'dist/', 'openapi.json', 'src/lib/api.generated.d.ts'],
	},
)
