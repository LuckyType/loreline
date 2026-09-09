/**
 * The bits-ui dropdown menu, vendored the way the dialog and the select above
 * it were: the shadcn-svelte "vega" files, with the `cn-menu-*` classes this
 * project never adopted stripped out, exactly as `select-content` has them
 * stripped.
 *
 * Only the parts something actually renders are here. The upstream component
 * also ships submenus, radio and checkbox groups, labels, separators and a
 * shortcut slot; vendoring those unused would be six more files nobody reads
 * and nobody keeps up to date. Add one back from the registry the day a caller
 * wants it, rather than carrying it against the day one might.
 */

import Content from './dropdown-menu-content.svelte'
import Item from './dropdown-menu-item.svelte'
import Portal from './dropdown-menu-portal.svelte'
import Trigger from './dropdown-menu-trigger.svelte'
import Root from './dropdown-menu.svelte'

export {
	Root,
	Trigger,
	Content,
	Item,
	Portal,
	//
	Root as DropdownMenu,
	Trigger as DropdownMenuTrigger,
	Content as DropdownMenuContent,
	Item as DropdownMenuItem,
	Portal as DropdownMenuPortal,
}
