// Configuration for commit-and-tag-version, which is how Loreline cuts a
// release. docs/CONTRIBUTING.md, "Cutting a release", covers how to run it;
// what follows is why it is shaped this way.
//
// The one thing to hold onto: this project's version lives in pyproject.toml,
// not in the package.json sitting next to this file. That package.json exists
// only so the release tool has somewhere to be installed from. Nearly every
// override below follows from that inversion, because the tool's defaults all
// assume it is releasing the npm package it lives inside, and it is not.

'use strict'

const PYPROJECT_UPDATER = 'scripts/version-pyproject.cjs'

module.exports = {
	// Where the current version is read from. Overriding this is not
	// cosmetic: the default is package.json, and the root package.json here
	// deliberately carries no version field, so leaving the default would have
	// the tool bump from a version that does not exist.
	packageFiles: [{ filename: 'pyproject.toml', updater: PYPROJECT_UPDATER }],

	// Where the new version is written. The default list is package.json and
	// the npm lockfiles, none of which describe this project, so it is replaced
	// rather than extended.
	//
	// On pyproject.toml specifically: commit-and-tag-version does ship a
	// built-in `python` updater that recognises the filename, and it would work
	// on today's file. It is not used, because it takes the first line anywhere
	// in the file matching a quoted `version = ...` with no idea which table
	// that line belongs to. This pyproject.toml already carries `[tool.uv]`,
	// `[tool.uv.sources]`, `[tool.hatch.*]` and others, and on the day one of
	// them grows a `version` key above line 3 the built-in quietly starts
	// editing that instead. The updater here is table-aware and throws when it
	// cannot find `[project].version`, which is the behaviour worth having in
	// something that runs unattended and then tags the result.
	bumpFiles: [
		{ filename: 'pyproject.toml', updater: PYPROJECT_UPDATER },

		// The frontend moves in lockstep with the backend. They ship in one
		// image and have no separate release life, so a split version would be
		// a number nobody could act on.
		{ filename: 'frontend/package.json', type: 'json' },

		// The frontend lockfile repeats that version in two places. `npm ci`
		// tolerates the disagreement, unlike uv, so this is not a broken build
		// waiting to happen; it is here so the next `npm install` in frontend/
		// does not silently rewrite the lockfile and strand a one line diff in
		// somebody's unrelated commit. The built-in json updater already knows
		// to write `packages[""].version` as well as the top level one.
		{ filename: 'frontend/package-lock.json', type: 'json' },

		// The one that bites, at a distance. scripts/version-uv-lock.cjs has
		// the full story; in short, `uv sync --frozen` is what the Dockerfile
		// runs, and it refuses to start when uv.lock's own `loreline` entry
		// disagrees with pyproject.toml.
		{ filename: 'uv.lock', updater: 'scripts/version-uv-lock.cjs' },
	],

	// Two files that look like they belong in the list above and deliberately
	// do not, recorded here because both have been argued for once already.
	//
	// src/loreline/__init__.py used to hold `__version__` as a literal, and
	// that is precisely what went wrong: nothing here bumped it, so the v0.2.0
	// image reported `v0.2.0` under Settings > Client and `0.1.0` in the
	// header. The fix was not to add a fifth entry. `__version__` now comes
	// from the installed distribution's metadata, which the build backend
	// fills from pyproject.toml, so the file at the top of this list is the
	// only one that has to move. A copy that tooling keeps in step is still a
	// copy, and the one that gets forgotten next time is the one nobody
	// remembered to add.
	//
	// frontend/openapi.json carries an `info.version`, which is the version of
	// the API the document describes rather than of the build that serves it.
	// It is pinned to a constant in src/loreline/web/app.py for that reason.
	// Bumping it here would put the release version back into a committed
	// generated file that scripts/check-openapi.sh and CI diff on every push,
	// which turns a forgotten `npm run gen:api` into a red build for everyone.

	// Stated rather than left to the default, because this value is load
	// bearing well outside this file. The deployed build reports
	// `git describe --tags --always` in Settings > Client, so the tag's spelling
	// is what an operator reads back when asked which revision is running.
	tagPrefix: 'v',

	// The changelog answers one question, "what changed in the build I am
	// running", so it carries the types that change behaviour and hides the
	// rest. The hidden ones are not busywork: there are thirty-odd `refactor`
	// and thirty-odd `docs` commits behind the first release and they were real
	// work. But nobody comparing two deployed revisions is served by "extract
	// the session header", and at that volume they would bury the entries that
	// do answer the question. Anyone who wants the full record has git log.
	//
	// `merge` is this repo's own invention rather than a conventional-commits
	// type: merge commits worth a sentence are written as `merge: <summary>`,
	// and there are more of those than there are features. They parse as a
	// perfectly valid type, so they have to be hidden by name. Ordinary
	// `Merge branch ...` commits need no entry here; they are dropped earlier,
	// by not parsing as a type at all.
	types: [
		{ type: 'feat', section: 'Features' },
		{ type: 'fix', section: 'Bug Fixes' },
		{ type: 'perf', section: 'Performance' },
		{ type: 'revert', section: 'Reverts' },
		{ type: 'refactor', hidden: true },
		{ type: 'docs', hidden: true },
		{ type: 'test', hidden: true },
		{ type: 'build', hidden: true },
		{ type: 'ci', hidden: true },
		{ type: 'chore', hidden: true },
		{ type: 'style', hidden: true },
		{ type: 'merge', hidden: true },
	],

	// There is deliberately no `preMajor` key here, and adding one would
	// achieve nothing. commit-and-tag-version sets that option itself, in
	// lib/lifecycles/bump.js:
	//
	//     if (semver.lt(currentVersion, '1.0.0')) presetOptions.preMajor = true;
	//
	// so for as long as Loreline sits below 1.0.0 the preset runs in its 0.x
	// mode whatever this file says, and that mode shifts every bump down a
	// step. A release full of features computes as a patch, and only a BREAKING
	// CHANGE reaches a minor. The reasoning is sound enough, 0.x promises
	// nothing to anyone, but it does mean the automatic bump is not the one
	// most people expect: 0.1.0 plus fifty-nine features proposed 0.1.1. The
	// first release was cut with `--release-as minor` for that reason, and
	// docs/CONTRIBUTING.md says when to reach for the flag again.

	scripts: {
		// Runs after the bump files are written and before anything is
		// committed or tagged, so a lockfile that failed to move aborts the
		// release here instead of surfacing as a failed image build days later.
		//
		// .pre-commit-config.yaml already runs `uv lock --check` when
		// pyproject.toml or uv.lock is staged, which does catch this, so this
		// hook can look redundant. It is not, on two counts: it fires before
		// the changelog is written rather than at commit time, so a failure
		// leaves less to unpick, and it belongs to the release itself rather
		// than to the hooks, so it still runs in a fresh clone where nobody has
		// run `uv run prek install` yet.
		// `--quiet` keeps the ordinary case silent: commit-and-tag-version
		// relays a hook's stderr as a warning, and uv writes "Resolved 77
		// packages" there even when everything is fine. A real failure still
		// prints its reason, and the non-zero exit stops the release.
		postbump: 'uv lock --check --quiet',
	},
}
