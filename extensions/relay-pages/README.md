# Relay Pages

Private reading pages for long CLI replies. An agent running behind the gateway writes
its long answer as Markdown, runs `relay-page publish`, and replies in Telegram with a
one-line summary and the page URL. Pages use the Relay Pages design system in `design/`
(exported from claude.ai).

This is not a direct command extension: it has no `manifest.json`, the gateway never
loads it, and the gateway core does not depend on it. It runs as its own small service
because it answers HTTP from the internet. Keeping that out of the gateway process means
a page-server problem cannot hold up Telegram dispatch, and gateway restarts do not take
pages offline.

| Part | Role |
|---|---|
| `relay_pages/render.py` | Markdown → sanitized HTML (markdown-it-py with GFM, nh3, Pygments) |
| `relay_pages/server.py` | stdlib HTTP server: login, pages, raw Markdown, images |
| `relay_pages/cli.py` | the `relay-page` command |
| `static/` | `app.css` (gaps in the export, English interface), `reader.js`, `theme.js`, icons |
| `design/` | the design-system export, untouched |
| `AGENT_RULE.md` | the instruction block for the CLIs |
| `systemd/relay-pages.service` | user unit |

## Setup

1. `cd extensions/relay-pages && uv sync`
2. Create `.env` here (ignored by Git):

   ```bash
   RELAY_PAGES_BASE_URL=https://pages.example.com
   RELAY_PAGES_PORT=8787
   RELAY_PAGES_DEFAULT_TTL=7d
   ```

   Bot tokens and the Bot API endpoint are read from the gateway's `.env`; they only
   decide which bot a page names and links back to.
3. Create an account: `.venv/bin/relay-page user set <name> --generate` prints the password.
4. Install the unit: copy `systemd/relay-pages.service` to `~/.config/systemd/user/`, then
   `systemctl --user enable --now relay-pages`.
5. Put a TLS reverse proxy in front of `127.0.0.1:8787`. The server listens on loopback
   only and rate-limits logins by `X-Real-IP`, so the proxy must set that header:

   ```nginx
   server {
       listen 443 ssl http2;
       server_name pages.example.com;
       # ssl_certificate / ssl_certificate_key …
       access_log off;
       client_max_body_size 16k;
       location / {
           proxy_pass http://127.0.0.1:8787;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
       }
   }
   ```
6. Put `relay-page` on the CLIs' `PATH`, for example a `/usr/local/bin/relay-page` script
   that runs `exec <this dir>/.venv/bin/relay-page "$@"`.
7. Append `AGENT_RULE.md`, with your domain in its example, to each CLI's global
   instructions: `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.grok/AGENTS.md`,
   `~/.pi/agent/APPEND_SYSTEM.md`. Claude, Grok and Pi read them on their next turn; a
   long-running Codex app-server may need a restart.

## Commands

```bash
relay-page publish reply.md            # prints <base URL>/p/<id>
relay-page publish - < reply.md --ttl forever --title "…"
relay-page list | delete <id>… | prune
relay-page user set <name> [--generate | --password-stdin]   # create, or reset a password
relay-page user list | delete <name>
relay-page serve                       # what the systemd unit runs
```

`publish` guesses the CLI (Claude/Codex/Grok/Pi) and the bot entrance from its process
tree and the gateway's `.runtime/state.json`; `--author` and `--bot` override the guess.
The first `# ` heading becomes the title. Local images referenced by absolute path, or by
a path relative to the Markdown file, are copied into the page.

## Access

Every page, its raw Markdown and its images need a session. Without one, `/p/<id>`
shows a username/password form that says nothing about the page, the same for ids
that do not exist. A correct login sets an HttpOnly, Secure, SameSite=Lax session
cookie for 90 days and returns to the page; browsers can save the password.

- Accounts are in `.runtime/users.json` (scrypt hashes, 0600) and change without a
  restart: `relay-page user set <name>` resets a password and logs that account out
  everywhere; `relay-page user delete <name>` removes it.
- Cookies are HMAC-signed with `.runtime/secret.key`. Deleting it and restarting the
  service logs every browser out.
- Five failed logins from one address lock it out for 15 minutes; 50 failures site-wide
  in 15 minutes pause all logins. Wrong passwords and unknown users get the same answer.
- Markdown is rendered with raw HTML disabled, cleaned by nh3 against an allowlist, and
  served with a CSP that allows no inline script. Responses are `no-store`, `noindex`.
- The app logs request paths without query strings.

## Storage

`.runtime/pages/<id>/{meta.json, body.html, source.md, files/}`, all 0600/0700 and ignored
by Git. Pages expire after `RELAY_PAGES_DEFAULT_TTL` (7 days). An hourly prune drops
expired content but keeps `meta.json` for 30 days so the link says it expired.

## Design gaps

`design/` is the export, untouched. `static/app.css` fills what it leaves out:

1. DESIGN.md names `components/bundle.css`; the stylesheet is `relay-pages.css`.
2. Block spacing (space-6) has no CSS rule; `example.html` fakes it with inline spacer divs.
3. Wrapped inline code split into open boxes on phones (`box-decoration-break: clone`).
4. `.rp-card` was content-box, so the text column was 720px (45 CJK characters, not ~40).
5. No styles for folded code blocks, task lists, footnotes, the login page or the list page.
6. The Tip callout label was 「完成/建议」; the renderer uses "Tip".

The interface is English rather than the export's Chinese. Its strings live in
`templates.py`, `render.py` (callout labels), `server.py` (login errors),
`static/reader.js` and the table-of-contents toggle in `static/app.css`. Page content is
whatever the agent writes; `AGENT_RULE.md` asks for English unless the user wants
another language.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

## Removal

`systemctl --user disable --now relay-pages`, remove the reverse-proxy site and its DNS
record, delete the `relay-page` wrapper, and take the `AGENT_RULE.md` block back out of
the four instruction files.
