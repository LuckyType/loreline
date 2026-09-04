#!/usr/bin/env bash
#
# Fail when the frontend's wire types no longer describe the backend.
#
# Two links in one chain, checked in order:
#
#   src/loreline/**.py  ->  frontend/openapi.json  ->  frontend/src/lib/api.generated.d.ts
#
# The first is the one a Python change breaks, so it is checked with uv alone
# and always runs. The second needs the generator from frontend/node_modules,
# which a backend-only checkout has no reason to have installed; it is skipped
# with a note there, and CI's frontend job (npm run check) covers it either way.
#
# Fix either failure with:  cd frontend && npm run gen:api
set -euo pipefail

cd "$(dirname "$0")/.."

document=frontend/openapi.json
generated=frontend/src/lib/api.generated.d.ts
current=$(mktemp)
trap 'rm -f "$current"' EXIT

uv run loreline openapi >"$current"
if ! diff -u "$document" "$current" --label "$document (committed)" --label "the API as it is now"; then
	echo
	echo "$document is out of date. Regenerate it: cd frontend && npm run gen:api"
	exit 1
fi

if [ ! -x frontend/node_modules/.bin/openapi-typescript ]; then
	echo "note: $generated not checked (frontend/node_modules is missing; run npm ci there)"
	exit 0
fi

if ! (cd frontend && npm run --silent check:api); then
	echo
	echo "$generated is out of date. Regenerate it: cd frontend && npm run gen:api"
	exit 1
fi
