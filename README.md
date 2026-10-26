# BombaMod

**Free, open-source Discord moderation powered by OpenAI Omni Moderation and Inception Mercury Decide.**

BombaMod is an MIT-licensed self-hosted bot. You can use, modify, and redistribute the software without paying a BombaMod license fee. It runs on a small CPU worker and starts with SQLite; no GPU or paid database is needed. You do need a Discord application/token and API keys. Third-party free quotas and hosting reliability are not guaranteed by BombaMod.

> **AI is fallible.** A moderation model is not a human moderator or a substitute for server safety policies. Keep actions off until your moderators have tested the system, use a review channel, and make an appeals/contact path available. BombaMod never automatically punishes on provider failure.

## What it does

- Classifies real-time server text with OpenAI's `omni-moderation-latest` model.
- Can optionally scan eligible images through the same OpenAI Moderation API. Image scanning is off by default.
- Asks a decision model (Inception **Mercury Decide**, ~0.3 s) to interpret severe moderation categories against your server rules, and (only after explicit server opt-in) best-effort-redacted flagged text.
- **Tolerates banter.** Only severe categories (hate, threats, self-harm, violence, sexual content involving minors) can trigger an action. Plain insults and roasting are allowed silently.
- **Handles spam efficiently.** Identical repeated messages reuse the first decision, deletes within 0.4 s are sent as one bulk delete, and moderator alerts are collapsed into a counted digest.
- **Tells people why.** A member who is deleted, timed out or banned gets one short DM (at most one per minute) naming the server and reason.
- Lets administrators manage rules and action/privacy limits with the `/bm` slash-command group.
- Supports moderator correction labels for offline evaluation. It does **not** silently train model weights.
- Stores guild settings and short-retention metadata in SQLite by default or PostgreSQL via `DATABASE_URL`. Message text, images, and prompts are not persisted.

### How policy setup relates to model training

`/bm rules` supplies moderator-authored server policy to the decision model on each decision. This is in-context policy, not fine-tuning: it does not change the model's parameters and does not make the model learn automatically from member actions. `/bm feedback` stores the message ID, moderator ID, predicted/corrected action and category labels for a case held in memory; only cases still available in that bot process can be labeled. Use labels to measure a separate offline test set and tune rules/thresholds. Labels are **not** used to retrain a model.

A responsible improvement loop is: start with clear written rules and automatic actions off; create synthetic examples for allowed/prohibited/ambiguous cases; have moderators label representative, de-identified cases; measure false positives and false negatives by category; revise the rules or confidence threshold; replay the test set; then pilot in review-only mode before enabling actions. Fine-tuning a model requires separate compute, data rights/consent, dataset governance, evaluation, and deployment. BombaMod does not do this in the bot process.

## Prerequisites

- Python 3.11 or newer (Docker uses Python 3.12).
- A Discord application with a bot token.
- An OpenAI API key for the Moderation API. OpenAI currently documents `/v1/moderations` as free to use, but account access/rate limits and provider terms still apply.
- An Inception API key (recommended) from [platform.inceptionlabs.ai](https://platform.inceptionlabs.ai) for Mercury Decide. An OpenRouter key also works (`inception/mercury-decide:free`) but its free tier rate-limits quickly. If the decision provider is unavailable, BombaMod escalates for moderator review instead of pretending content passed.

## Discord setup

1. Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent**. BombaMod needs message text/attachments to moderate; Discord may require additional review/approval for verified applications.
3. Invite the bot to your server with **View Channels**, **Send Messages**, **Use Application Commands**, and **Read Message History**. For autonomous deletion also grant **Manage Messages**; for timeouts grant **Moderate Members**; for optional bans grant **Ban Members**. Avoid Administrator permission. Put the bot role above members it may timeout/ban; Discord role hierarchy still blocks actions it cannot perform.
4. Run `/bm setup` in the server. Optionally pass comma-separated channel mentions/IDs. BombaMod remains inert until setup enables the guild.
5. Use `/bm rules` to provide short, explicit server rules. Use `/bm review-channel` to send review and audit notices to a moderator-only channel.
6. Start with `/bm enforcement enabled:false`, `/bm privacy share_text:false scan_images:false`, then `/bm status`. Use review-only operation to validate category signals before enabling actions.

## Local install

```bash
python -m venv .venv
. .venv/bin/activate                 # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
cp .env.example .env                 # Windows: copy .env.example .env
# Add DISCORD_TOKEN, OPENAI_API_KEY, and DECISION_API_KEY to .env
bombamod
```

Keep `.env` private. Never paste tokens into public channels or commit credentials. If a token is exposed, revoke and rotate it immediately. On startup the bot refuses to run when required credentials are missing. It creates `./data/bombamod.db` when `DATABASE_URL` is empty.

Global slash-command changes may take a while to appear. Set `DISCORD_GUILD_ID` in `.env` to sync commands to one development guild immediately.

## Slash commands

Commands under `/bm` are restricted to moderators (Manage Messages/Moderate Members) or server managers. `/bm setup`, `/bm enforcement`, `/bm privacy`, `/bm review-channel`, and `/bm pause` require **Manage Server** or **Administrator**; only a server manager may enable external content sharing or high-impact actions:

| Command | Purpose |
| --- | --- |
| `/bm setup [channels]` | Enable moderation and optionally limit monitored channels. Empty means every channel, so configure private/opt-out channels as needed. Uses `IMAGE_SCAN_ENABLED_BY_DEFAULT` for the guild's initial image setting. |
| `/bm rules text` | Set moderator-authored policy (up to 6000 chars); this is decision context, not model training. |
| `/bm enforcement enabled max_timeout_minutes min_confidence allow_bans` | Configure actions. Bans require a separate explicit opt-in. |
| `/bm privacy share_text scan_images` | Independently opt into best-effort-redacted flagged text sent to OpenRouter and image attachments sent to OpenAI. Both are off by default. |
| `/bm review-channel [channel]` | Choose where moderator review and action notices go. |
| `/bm logging enabled` | Opt in to showing the author and a short excerpt (200 characters) of each deleted, timed-out, banned or escalated message in the review channel. Off by default; keep that channel moderator-only. Identical spam collapses into one counted line. |
| `/bm feedback message_id corrected_action` | Label a recent in-memory case without storing content. Only scanned cases still in process memory are labelable. |
| `/bm export-feedback` | Export retained labels without Discord IDs or raw content; treat exports as private. |
| `/bm retention strike_days feedback_days` | Set metadata retention windows (1–365 days). |
| `/bm pause` | Immediately disable new moderation work for the server. |
| `/bm status` | Show current guild settings and recent feedback-label count. |

## Privacy and safety

- **OpenAI:** Message text is sent to OpenAI for classification whenever moderation is enabled, unless the operator sets `DISABLE_OPENAI_TEXT=true` (in which case every eligible message is escalated rather than treated as cleared). If opted in, enabled images are fetched from Discord's HTTPS CDN and sent with the text to OpenAI. The official model accepts text/images (not audio) and supports images up to 20 MB; BombaMod uses a configurable lower download cap by default. OpenAI documents API content as not used for model training by default, but says abuse-monitoring logs may retain content up to 30 days. See [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data).
- **Child safety:** OpenAI says **do not send known or suspected child sexual abuse material (CSAM) to its Moderation API**. Omni Moderation is not a CSAM detector; its `sexual/minors` category is text-only and will not identify sexual/minor content in images. Never enable image scanning for reported/suspected CSAM. Follow Discord/platform reporting and applicable law; preserve evidence only through your approved safety process, not by sending it to this service.
- **Decision provider (Inception/OpenRouter):** By default BombaMod sends the decision model the rules, category flags/scores, and bounded strike count, but **not** message text. A server admin must explicitly set `/bm privacy share_text:true` to also send best-effort-redacted flagged text. Redaction removes common mentions, identifiers, contact details and links, but is not a guarantee against personal or confidential information. Review the provider's retention and privacy terms before opting in; do not enable text sharing for sensitive/private communities unless you accept them.
- **Local data:** Raw message text, images, model prompts, and message contents are never written to the BombaMod database or application logs. The one opt-in exception is `/bm logging enabled:true`, which posts a short excerpt to your private Discord review channel (Discord, not BombaMod, then holds that copy). Stored Discord IDs and action/category labels are still personal data and need an appropriate privacy notice and retention policy. Strike metadata expires on use after its configured 1–365 day window; feedback metadata is pruned on writes after its configured 1–365 day retention window. Retention is configurable with `/bm retention`. SQLite is local and is lost if your host's filesystem is ephemeral. Backups are the operator's responsibility.
- **Actions:** Automatic actions are disabled by default. Decisions are schema-validated and bounded by server settings; bot permissions and Discord's role hierarchy are checked. Ban opt-in is separate and disabled by default. No system can guarantee correct AI actions; have moderators monitor and provide an appeal process.
- **Prompt injection:** Member content is untrusted input. The decision prompt instructs the model to treat it as data, but prompt instructions do not guarantee model behavior. Code, not the model, enforces action limits. Do not rely on AI for emergencies, threat response, self-harm support, or legal determinations.

## Configuration reference

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | Yes | — | Discord bot token. |
| `OPENAI_API_KEY` | Yes | — | OpenAI Moderation API key. |
| `DECISION_API_KEY` | Yes | — | Key for the decision provider (Inception, or OpenRouter). Legacy name `OPENROUTER_API_KEY` still works. |
| `DECISION_BASE_URL` | No | `https://openrouter.ai/api/v1` | Use `https://api.inceptionlabs.ai/v1` for Inception's official API (recommended). |
| `DECISION_MODEL` | No | `inception/mercury-decide:free` | `mercury-decide` on Inception, `inception/mercury-decide:free` on OpenRouter. Names containing `decide`/`jev` use the Decisions API; others use chat completions. |
| `DATABASE_URL` | No | local SQLite | `postgresql://...` is normalized to async PostgreSQL. Use secret-manager variables in hosting. |
| `DISCORD_GUILD_ID` | No | global commands | Optional development guild for fast command registration. |
| `MAX_CONCURRENT_MODERATION` | No | `4` (setup script writes `8`) | Bound concurrent message processing (1–64). |
| `HTTP_TIMEOUT_SECONDS` | No | `12` | Per-request network timeout (3–60). |
| `MAX_IMAGE_BYTES` | No | `8000000` | Total image bytes per case (100 KB–20 MB), subject to provider support. |
| `MAX_MESSAGE_CHARS` | No | `6000` | Maximum text sent for classification; longer messages are escalated (1,000–20,000). |
| `IMAGE_SCAN_ENABLED_BY_DEFAULT` | No | `false` | Initial per-guild image-scan setting; command can toggle per server. |
| `ALLOW_OPENROUTER_TEXT` | No | `false` | Global hard gate for OpenRouter text; even true requires per-server `/bm privacy` opt-in. |
| `DISABLE_OPENAI_TEXT` | No | `false` | If true, do not transmit message text to OpenAI; all cases are escalated for moderator review. |
| `LOG_LEVEL` | No | `INFO` | Standard Python logging level. |

## Production deployment (Docker on a VM)

BombaMod is a **persistent worker** (Discord's Gateway is a WebSocket), not a serverless function. It needs no inbound port and no GPU; any small Linux VM with Docker works.

### 1. First-time setup

```bash
git clone https://github.com/koryei/BombaMod.git
cd BombaMod
./scripts/setup-env.sh      # asks for each key (hidden input), tests them, writes .env (chmod 600)
./scripts/update.sh         # builds and starts the container, then follows the logs
```

Where each value comes from:

| Variable | Where to get it |
| --- | --- |
| `DISCORD_TOKEN` | [Developer Portal](https://discord.com/developers/applications) → your app → **Bot** → **Reset Token**. Also enable **Message Content Intent** on that page. |
| `OPENAI_API_KEY` | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) (the moderation endpoint is free). |
| `DECISION_API_KEY` | [platform.inceptionlabs.ai](https://platform.inceptionlabs.ai) → API keys. |
| `DECISION_BASE_URL` / `DECISION_MODEL` | `https://api.inceptionlabs.ai/v1` and `mercury-decide` (the script sets these). |

Prefer to edit by hand? `cp .env.example .env`, fill in the blanks, then `chmod 600 .env`. Values are plain `KEY=value` with no quotes or spaces. `.env` is git-ignored and never leaves the server; if a token ever leaks, reset it in the provider's dashboard and re-run `./scripts/setup-env.sh`.

A healthy start logs `BombaMod connected` and `Decision model: mercury-decide via https://api.inceptionlabs.ai/v1`. If the bot exits right away, the log names the missing variable.

### 2. Configure the server (in Discord)

1. Invite the bot with **View Channels, Send Messages, Read Message History, Manage Messages, Moderate Members** (add **Ban Members** only if you want bans). Put the bot's role **above** the members it may act on; Discord blocks actions on anyone with an equal or higher role.
2. Create a private channel (hidden from `@everyone`, visible to the bot) and run `/bm review-channel`.
3. `/bm rules` with short, explicit rules, then `/bm setup`.
4. `/bm enforcement enabled:true` once you trust it. Run `/bm status` to confirm. Until enforcement is on, flagged cases only go to the review channel.

### 3. Updating

Push to `main`, then on the server:

```bash
cd ~/BombaMod && ./scripts/update.sh
```

This pulls, rebuilds, restarts, and tails the logs. Settings live in the `bombamod-data` Docker volume and survive updates. Optional shortcut: `echo "alias bmup='cd ~/BombaMod && ./scripts/update.sh'" >> ~/.bashrc`.

### Operations

- Logs: `docker compose logs -f bombamod`. Stop: `docker compose down` (the data volume is kept). Back up the volume before removing it.
- Every message logs one line (`not flagged`, `banter, allowed`, or `action=...`) without its text.
- The Compose file runs the container read-only, non-root, with all Linux capabilities dropped, restarts it on failure, and rotates logs.
- For PostgreSQL, set `DATABASE_URL` in `.env`. Tables are created on startup; there is no migration framework yet, so back up before upgrading. Run only one active bot per SQLite database.
- Free hosting is best effort: messages sent while the bot is offline are not scanned. Use an always-on VM and keep Discord AutoMod enabled as a backup.

### Troubleshooting

| Symptom | Cause |
| --- | --- |
| Nothing happens, `/bm status` shows `Moderation enabled: False` | Run `/bm setup` (needs a review channel first). |
| Flagged cases only appear in the review channel | `/bm enforcement enabled:true` is off. |
| `Discord action failed` alert | The bot lacks Manage Messages / Moderate Members, or the member's top role is not below the bot's. |
| `Decision request failed ... status=401` | Wrong `DECISION_API_KEY`; re-run `./scripts/setup-env.sh`. |
| `... status=404` or `400` | Wrong `DECISION_BASE_URL` or `DECISION_MODEL`. |
| `... status=429` | Provider rate limit; use the Inception API instead of OpenRouter's free tier. |
| `Private moderator alert channel is unavailable` | The review channel is visible to `@everyone` or the bot cannot view/send there. |

## Development and checks

```bash
python -m pip install -e '.[dev]'
ruff check .
ruff format --check .
mypy src/bombamod
pytest
```

Tests mock external services; running them does not contact Discord, OpenAI, or OpenRouter. For a live pilot, use a private test server and clearly notify participants which services receive message data.

## License

BombaMod is distributed under the [MIT License](LICENSE). The software is free to use; Discord, OpenAI, OpenRouter, and hosting services are separate services with their own terms, eligibility, quotas, and availability.
