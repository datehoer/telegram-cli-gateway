# Article extension

`/article` reads one web page with an isolated Chromium profile, loads the bundled
Bypass Paywalls Clean extension, extracts readable text with Mozilla Readability,
and returns a Markdown file through the gateway's existing artifact handling.
It is a direct command: it does not start an AI CLI turn or consume model tokens.

## Usage

```text
/article https://example.org/article
/article https://example.org/article --archive
/article https://archive.ph/abcde
/article help
```

The original URL is tried first. WSJ links automatically fall back to existing
archive.ph and archive.is snapshots when the original cannot be read.
`--archive` enables the same fallback for other sites. The command only looks
for existing snapshots; it does not submit a page for archiving.

A successful response includes a `.md` artifact path. A companion `.json` file
records the source, retrieved URL, retrieval method, paragraph/word counts, and
attempt results. Both are saved under `/srv/projects/artifacts` by the manifest.
The Markdown contains the article title, source links, and retrieval time.

Challenge pages, HTTP failures, visible paywalls, BPC failure notices, archive
search listings, and short fragments are rejected. For WSJ, merely removing the
paywall overlay does not count as successful archive restoration. These checks
are heuristics: a successful extraction does not independently prove that every
paragraph of the original publication is present.

## Install and enable

Requires Node.js 22 or later and a machine capable of running Chromium.

```bash
cd /srv/projects/telegram-cli-gateway/extensions/article
npm ci --ignore-scripts --no-audit --no-fund
npm run install-browser
TG_ARGV0=help node main.mjs
```

Dependencies and Chromium were installed in this workspace during development.
Playwright uses its full Chromium build because the bundled BPC browser extension
requires extension support. `ARTICLE_CHROMIUM_EXECUTABLE` can select a compatible
existing Chromium binary if a deployment cannot use Playwright's cached build.

The gateway discovers `manifest.json` on startup; adding this directory requires
a gateway restart. Development and verification did not restart the running
gateway. Coordinate a restart after its current work has finished; changes to
other gateway files would also take effect on that restart.

## Operation

- The gateway provides raw input in `TG_RAW_ARGS`; its first argument can also
  arrive only in `TG_ARGV0`. Both paths are supported without shell execution.
- `@@PROGRESS` lines report browser startup, extraction, and archive lookup.
- Each invocation uses a fresh temporary browser profile, with no saved login
  session. The browser and profile are closed and removed after the attempt.
- Navigation is limited to 20 seconds per page, with a 110-second capture budget
  and a 180-second gateway process timeout. Failure returns a nonzero exit code
  without announcing a successful article artifact.
- CAPTCHA is detected and reported. The command does not solve it.

## Verification on 2026-10-05

Three real WSJ URLs all returned HTTP 401 with a DataDome challenge. Both archive
fallback hosts timed out from the tested server route. Standard user-agent and
headed Chromium checks also encountered the WSJ challenge. No WSJ body was
extracted. A public article control succeeded: 62 paragraphs and 4,385 words.
These observations describe the tested access routes and date, not global site
availability.

```bash
# Extension tests: real Chromium with original, locally supplied page fixtures
npm test

# Gateway manifest and CLI-contract tests
cd /srv/projects/telegram-cli-gateway
PYTHONPATH=src python3 -m unittest discover -s tests -p test_article_extension.py
```

The extension has nine unit/entry-point tests and three browser regression tests.
Browser tests cover a normal article, WSJ challenge followed by a successful
archive snapshot, and failures at both original and archive URLs. They verify
the local implementation; the synthetic archive success is separate from the
failed live WSJ tests. The full gateway suite passed with 364 tests, three skipped.

## Bundled source

BPC is pinned to version **4.4.6.0**, with upstream files and license retained in
`vendor/bpc`. Its download URL and ZIP SHA-256 are in
`vendor/bpc-provenance.json`. It is not downloaded or updated during a request;
the browser extension's opt-in updater is disabled for each fresh profile.
To update it, review a new upstream ZIP, replace the bundled files, update the
provenance and reported version in `main.mjs`, then rerun the browser tests.

- [Bypass Paywalls Clean project](https://gitflic.ru/project/magnolia1234/bypass-paywalls-chrome-clean)
- [Playwright browser extension documentation](https://playwright.dev/docs/chrome-extensions)
- [Mozilla Readability](https://github.com/mozilla/readability)
- [Turndown](https://github.com/mixmark-io/turndown)
