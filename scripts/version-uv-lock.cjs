// A commit-and-tag-version updater for uv.lock.
//
// This one exists because of a failure that stays silent until it reaches a
// build. uv.lock carries a `[[package]]` entry for loreline itself, holding a
// copy of the version, and `uv sync --frozen` refuses to run when that copy
// disagrees with pyproject.toml. The Dockerfile uses `uv sync --frozen`. So a
// release that bumped pyproject.toml and left uv.lock alone would commit and
// tag perfectly cleanly, and then fail every image build from that tag on.
//
// The obvious alternative is to run `uv lock` during the release and let uv
// update its own file. That was rejected for a reason that is easy to miss:
// commit-and-tag-version stages exactly the files it was told to bump, plus
// the changelog. A `uv lock` run from a lifecycle hook would leave uv.lock
// modified in the working tree but outside the release commit, so the tag
// would still point at a tree whose lock disagrees with pyproject.toml, which
// is the exact bug being avoided. Getting uv.lock into the commit means
// listing it in `bumpFiles`, and `bumpFiles` needs an updater. Rewriting one
// field is also hermetic in a way a resolver is not: no network, no
// dependency on the uv version, and no exposure to `[tool.uv] exclude-newer`,
// which is a window that moves as the calendar does. uv still gets the final
// word, because `.versionrc.cjs` runs `uv lock --check` as a postbump gate.
//
// Finding the right field is the whole difficulty here. uv.lock opens with
//
//     version = 1
//
// which is the *lockfile format* version, and the first `[[package]]` block
// after it belongs to whichever dependency sorts first alphabetically. An
// unanchored search for `version = ` finds the format marker; a search for the
// first quoted version finds a dependency. Either would be a quiet disaster,
// corrupting the lockfile's format marker or silently repinning an unrelated
// package. So this walks the file, finds the `[[package]]` block whose `name`
// is loreline, and edits only the `version` key belonging to that block.

'use strict'

// The package whose version tracks the project's. This is the `name` under
// `[project]` in pyproject.toml; if that is ever renamed, this must follow.
const PACKAGE_NAME = 'loreline'

const PACKAGE_HEADER = /^\[\[package\]\]\s*$/

// Any table header at column zero ends the current package's own keys, whether
// it is the next `[[package]]` or a sub-table such as `[package.metadata]`.
const TABLE_HEADER = /^\[/

// `name` and `version` as direct keys of a `[[package]]`. Both are anchored at
// column zero on purpose: the entries of the `dependencies` and `wheels`
// arrays are indented and carry their own `name` and `url` keys, and an
// unanchored match would happily read a dependency's name as the block's.
const NAME = /^name\s*=\s*"([^"]*)"\s*$/
const VERSION = /^(version\s*=\s*")([^"]*)(".*)$/

function locatePackageVersion(contents) {
	const lines = contents.split('\n')
	let index = 0

	while (index < lines.length) {
		if (!PACKAGE_HEADER.test(lines[index])) {
			index += 1
			continue
		}

		let name = null
		let versionIndex = -1
		let scan = index + 1

		for (; scan < lines.length && !TABLE_HEADER.test(lines[scan]); scan += 1) {
			const named = lines[scan].match(NAME)
			if (named) {
				name = named[1]
				continue
			}

			if (versionIndex < 0 && VERSION.test(lines[scan])) versionIndex = scan
		}

		if (name === PACKAGE_NAME && versionIndex >= 0) {
			const version = lines[versionIndex].match(VERSION)
			return {
				lines,
				index: versionIndex,
				prefix: version[1],
				value: version[2],
				suffix: version[3],
			}
		}

		// Resume from the header that ended this block, so it is examined in
		// its own right rather than skipped.
		index = scan
	}

	return null
}

// As in the pyproject updater, a miss throws. Silently leaving uv.lock at the
// old version is the failure this whole file exists to prevent, so it must not
// be the thing that happens when the format changes under us.
function required(contents) {
	const found = locatePackageVersion(contents)
	if (found) return found

	throw new Error(
		`uv.lock has no [[package]] entry named "${PACKAGE_NAME}" with a version; refusing to leave the lockfile behind the project version.`,
	)
}

module.exports.readVersion = function readVersion(contents) {
	return required(contents).value
}

module.exports.writeVersion = function writeVersion(contents, version) {
	const found = required(contents)
	found.lines[found.index] = `${found.prefix}${version}${found.suffix}`
	return found.lines.join('\n')
}
