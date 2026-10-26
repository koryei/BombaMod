# BombaMod

Discord moderation bot (discord.py + SQLite/Postgres), run with Docker on a VM.

## Pipeline
1. `bot.on_message` queues messages (guild must have run `/bm setup`, needs a private review channel).
2. OpenAI `omni-moderation-latest` classifies the text -> category scores.
3. Roast guard (`enforcement.SEVERE_CATEGORIES`): only severe categories (hate, threats, self-harm,
   violence, sexual/minors) can trigger action. Plain harassment/insults are allowed silently.
4. Decision model via OpenRouter (`providers.OpenRouterProvider`). Default
   `inception/mercury-decide:free` uses the Decisions API (`/api/alpha/decisions`, ~0.3s). Other models
   use chat completions with reasoning disabled. The model only sees scores unless text sharing is on.
5. `DiscordActionSink` applies delete/warn/timeout/ban, then DMs the user once per 60s.

## Spam handling
- Identical messages from one user reuse the first decision for 120s (`service._recent`).
- Deletes within 0.4s are one bulk delete (`DiscordActionSink._delete`).
- Review alerts are batched for 20s into one counted digest (`bot._format_alert_digest`).

## Deploy
Push to `main`, then on the VM run `bmup` (git pull + docker compose up -d --build).
`.env` and `data/` are gitignored. Settings live in the `bombamod-data` Docker volume.

## Checks
`PYTHONPATH=src .venv/bin/python -m pytest -q`, `ruff check src tests tools`, `mypy src`.

## Gotchas
- Actions fail silently-ish ("Discord action failed" alert) if the target's top role >= the bot's role.
- Autonomous actions default off: `/bm enforcement enabled:true`.
