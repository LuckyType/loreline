// A commit-and-tag-version updater for pyproject.toml, which is where
// Loreline's version actually lives. The tool ships updaters for JSON, YAML
// and plain text, but none for TOML, so the project's own version field has to
// be found by hand.
//
// The tempting way to do that is a regex for the first `version = "..."` in
// the file. That is wrong the moment the file grows. pyproject.toml already
// carries `requires-python`, dependency specifiers and several `[tool.*]`
// tables, and any future one of those could hold a `version` key of its own,
// at which point a first-match search starts editing somebody else's setting
// and says nothing about it. So this walks the file table by table and only
// ever touches the `version` key sitting directly under `[project]`, the one
// hatchling and uv read.

'use strict'

// A TOML table header at the start of a line, in both of its forms: `[project]`
// and the array-of-tables `[[package]]`. The capture is the dotted path, so
// `[project.optional-dependencies]` reads as `project.optional-dependencies`
// and is correctly not mistaken for `[project]` itself.
const TABLE_HEADER = /^\s*\[\[?([^[\]]+)\]\]?\s*$/

// A `version = "..."` assignment, anchored at column zero. Inside `[project]`
// the only things at column zero are keys of that table; anything indented
// belongs to a multi-line array such as `dependencies`. The trailing capture
// keeps any comment on the line intact.
const VERSION = /^(version\s*=\s*")([^"]*)(".*)$/

function locateProjectVersion(contents) {
	const lines = contents.split('\n')
	let table = null

	for (let index = 0; index < lines.length; index += 1) {
		const header = lines[index].match(TABLE_HEADER)
		if (header) {
			table = header[1]
			continue
		}

		if (table !== 'project') continue

		const version = lines[index].match(VERSION)
		if (version) {
			return {
				lines,
				index,
				prefix: version[1],
				value: version[2],
				suffix: version[3],
			}
		}
	}

	return null
}

// Both entry points throw rather than returning a default or quietly doing
// nothing. A release that cannot find the version is a release that would
// otherwise tag a tree it never bumped, which is far more expensive to notice
// later than a failed release is to notice now.
function required(contents) {
	const found = locateProjectVersion(contents)
	if (found) return found

	throw new Error(
		'pyproject.toml declares no `version` under [project]; refusing to guess where the project version lives.',
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
