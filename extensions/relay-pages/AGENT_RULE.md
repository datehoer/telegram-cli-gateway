## Long replies → Relay Pages

Telegram splits long messages and shows wide tables, long code and logs badly. When your final reply would be longer than about 2,500 characters, or would carry a table wider than 4 columns, code longer than about 30 lines, or a long log, publish it as a private reading page instead:

1. Write the complete reply as Markdown to a file (any path, e.g. `/tmp/reply-<topic>.md`). Start with `# <title>`. Lead with the conclusion; put anything the user must act on first, as `> [!WARNING]` callouts (`> [!CAUTION]` for risky or destructive steps); full logs go last. Local images (`![alt](/srv/projects/...png)`) are copied into the page.
2. Run `relay-page publish <file>`. It prints the page URL. Pages are password-protected and expire after 7 days; `--ttl forever` keeps one, `--title "..."` overrides the title.
3. The final chat reply is then only: a one-sentence summary (at most 25 words) + the URL, plus at most one line of key numbers. Example: `Deploy finished; 2 of 14 tasks need you → https://pages.example.com/p/k3x9a…`
4. Files the user should receive still follow the artifact rules: list their absolute `/srv/projects/...` paths in the chat reply too, not only inside the page.

Write the page and the summary in English unless the user asks for another language. Do not publish short answers, questions to the user, or replies that are mainly a file delivery. If `relay-page` fails, reply normally.
