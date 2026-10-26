#!/usr/bin/env bash
# Interactive .env creator: prompts for each secret (hidden input), tests the keys, writes .env (0600).
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  read -rp ".env already exists. Replace it (a backup is kept)? [y/N] " answer
  [[ $answer == [yY] ]] || { echo "Nothing changed."; exit 0; }
  cp .env ".env.bak.$(date +%s)"
fi

ask_secret() {  # ask_secret VAR "prompt"
  local value
  while true; do
    read -rsp "$2: " value; echo
    if [[ $value =~ ^[A-Za-z0-9._~+/=-]+$ ]]; then break; fi
    echo "  Paste the bare value only (no spaces or quotes), try again."
  done
  printf -v "$1" '%s' "$value"
}

# Calls curl with the secret header passed via stdin so it never shows in `ps`.
http_status() {  # http_status "Authorization: Bearer xyz" curl-args...
  local header=$1; shift
  curl -s -o /dev/null -w '%{http_code}' --max-time 15 -K - "$@" <<<"header = \"$header\"" || echo 000
}

check() {  # check "label" expected_status actual_status
  if [[ $3 == "$2" ]]; then echo "  ✓ $1 works"; else echo "  ✗ $1 failed (HTTP $3) - re-check that value"; fi
}

echo "== Discord =="
echo "Developer Portal -> your application -> Bot -> Reset Token."
ask_secret DISCORD_TOKEN "DISCORD_TOKEN"
echo "== OpenAI (moderation API) =="
echo "https://platform.openai.com/api-keys"
ask_secret OPENAI_API_KEY "OPENAI_API_KEY"

echo "== Decision model =="
echo "  1) Inception official API - mercury-decide (recommended, fast, high limits)"
echo "  2) OpenRouter - inception/mercury-decide:free (rate-limits quickly)"
read -rp "Choose [1]: " provider
if [[ ${provider:-1} == 2 ]]; then
  DECISION_BASE_URL=https://openrouter.ai/api/v1
  DECISION_MODEL=inception/mercury-decide:free
  echo "https://openrouter.ai/keys"
else
  DECISION_BASE_URL=https://api.inceptionlabs.ai/v1
  DECISION_MODEL=mercury-decide
  echo "https://platform.inceptionlabs.ai (API keys)"
fi
ask_secret DECISION_API_KEY "DECISION_API_KEY"

read -rp "DISCORD_GUILD_ID for instant slash commands in one server (optional, Enter to skip): " DISCORD_GUILD_ID
if [[ -n $DISCORD_GUILD_ID && ! $DISCORD_GUILD_ID =~ ^[0-9]+$ ]]; then
  echo "  Guild ID must be digits only; skipping."; DISCORD_GUILD_ID=
fi

if [[ ${SKIP_CHECKS:-0} != 1 ]] && command -v curl >/dev/null; then
  echo "== Testing keys =="
  check "DISCORD_TOKEN" 200 "$(http_status "Authorization: Bot $DISCORD_TOKEN" https://discord.com/api/v10/users/@me)"
  check "OPENAI_API_KEY" 200 "$(http_status "Authorization: Bearer $OPENAI_API_KEY" \
    -H 'Content-Type: application/json' -d '{"model":"omni-moderation-latest","input":"hello"}' \
    https://api.openai.com/v1/moderations)"
  if [[ $DECISION_BASE_URL == *inceptionlabs.ai* ]]; then
    decisions_url=$DECISION_BASE_URL/decisions
  else
    decisions_url=https://openrouter.ai/api/alpha/decisions
  fi
  check "DECISION_API_KEY" 200 "$(http_status "Authorization: Bearer $DECISION_API_KEY" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$DECISION_MODEL\",\"state\":\"hi\",\"questions\":{\"q\":{\"type\":\"noul\",\"instructions\":\"Is this a greeting?\"}}}" \
    "$decisions_url")"
fi

umask 177
cat > .env <<ENVFILE
DISCORD_TOKEN=$DISCORD_TOKEN
OPENAI_API_KEY=$OPENAI_API_KEY
DECISION_API_KEY=$DECISION_API_KEY
DECISION_BASE_URL=$DECISION_BASE_URL
DECISION_MODEL=$DECISION_MODEL
DISCORD_GUILD_ID=$DISCORD_GUILD_ID
MAX_CONCURRENT_MODERATION=8
LOG_LEVEL=INFO
ENVFILE
echo "Wrote .env (permissions 600). Next: ./scripts/update.sh"
