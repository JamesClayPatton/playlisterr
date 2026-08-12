---
name: Bug report
about: Something isn't working the way it should
title: ''
labels: bug
assignees: ''
---

**What happened**
A clear description of the bug.

**What you expected**
What you thought should happen instead.

**Steps to reproduce**
1. …
2. …

**Does it happen in the demo?**
Run `python -m playlisterr serve --demo` (or `run --offline`) — does the bug
reproduce there? This tells us whether it's a Plex/model issue or the app.

**Setup**
- Playlisterr version: (shown in the header / `System` page)
- Install: Docker / source / bare-metal
- LLM provider: ollama / openai / custom
- Delivery mode: playlist / collection

**Logs**
Relevant lines from **System → Download log** (they're already redacted of
tokens, but skim before pasting). Please don't paste your Plex token or API
keys.
