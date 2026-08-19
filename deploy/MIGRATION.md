# Archon — one-time migration runbook

Do these in order. Steps marked **[YOU]** need the owner personally (~15 min
total); everything else is automated or done by Claude.

## 0. Prerequisites gathered on Windows

| What | Where it comes from |
|---|---|
| `TELEGRAM_BOT_TOKEN` | **[YOU]** @BotFather → /newbot. Then /mybots → the bot → Bot Settings → **Business Mode → Turn on** |
| `TELEGRAM_OWNER_ID` | **[YOU]** message @userinfobot (or the new bot logs it on first /start) |
| Log channel id | **[YOU]** create a private channel, add the new bot as admin; forward a post to @userinfobot to get the `-100…` id |
| `TELEGRAM_API_ID` / `API_HASH` | **[YOU]** https://my.telegram.org → API development tools |
| `TELETHON_SESSION` | run `uv run python scripts/telethon_login.py` — **[YOU]** enter the code Telegram sends |
| Google token | run `uv run python scripts/google_consent.py` — **[YOU]** click through consent once (check the OAuth consent screen is "In production" first, else tokens die weekly) |
| LLM key(s) | whichever provider is active (GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENROUTER_API_KEY) |
| OCI API key | **[YOU]** OCI Console → profile icon → My profile → Tokens and keys → Add API key → Generate → download the .pem, copy the config snippet |

## 1. Provision the VM

```bash
# ~/.oci/config from the console snippet; key file path inside it must match
pip install oci-cli
export COMPARTMENT_ID=<tenancy OCID from the snippet>
bash deploy/provision_oci.sh          # retries A1 capacity across ADs
```

If capacity stays unavailable for days: upgrade the Oracle account to
Pay-As-You-Go (still $0 within Always-Free limits) — availability improves.

Then place the deploy key + clone:

```bash
scp -i ~/.ssh/archon_vm ~/.ssh/archon_deploy ubuntu@$IP:~/.ssh/archon_deploy
ssh -i ~/.ssh/archon_vm ubuntu@$IP 'chmod 600 ~/.ssh/archon_deploy && bash bootstrap.sh'
```

(`gh repo deploy-key add ~/.ssh/archon_deploy.pub -R ngelkind/archon` was done
when the repo was created.)

## 2. Secrets to the VM

On Windows, fill `.env` from `.env.example`, then:

```bash
scp -i ~/.ssh/archon_vm .env ubuntu@$IP:/opt/archon/secrets/.env
scp -i ~/.ssh/archon_vm -r secrets/google ubuntu@$IP:/opt/archon/secrets/
ssh -i ~/.ssh/archon_vm ubuntu@$IP 'chmod 600 /opt/archon/secrets/.env'
```

## 3. GitHub Actions secrets

```bash
gh secret set VM_HOST -R ngelkind/archon --body "$IP"
gh secret set VM_SSH_KEY -R ngelkind/archon < ~/.ssh/archon_deploy
ssh-keyscan -H "$IP" | gh secret set VM_KNOWN_HOSTS -R ngelkind/archon
```

From now on every push to main deploys.

## 4. First start (without WhatsApp)

```bash
ssh -i ~/.ssh/archon_vm ubuntu@$IP 'sudo systemctl start archon && sleep 5 && systemctl status archon --no-pager'
```

Send `/status` to the bot. Gmail + Calendar + control bot should be live.

## 5. WhatsApp session handover (LAST — the old bot stops here)

1. **[YOU]** Stop `your-other-project` (Ctrl+C in its console; check
   with `Get-Process python`). Verify no `data\session.db-journal` remains.
2. Copy the session:
   ```bash
   scp -i ~/.ssh/archon_vm \
     /c/Users/YOURUSER/PycharmProjects/your-other-project/data/session.db \
     ubuntu@$IP:/opt/archon/secrets/wa/session.db
   ```
3. Rename the old folder so the old bot can't be started by habit:
   `data` → `data_MIGRATED_DO_NOT_RUN` (two clients on one session =
   StreamReplaced fights).
4. Build goneonize from source and restart:
   ```bash
   ssh -i ~/.ssh/archon_vm ubuntu@$IP \
     'cd /opt/archon/app && bash deploy/build_goneonize.sh && sudo systemctl restart archon'
   ```
5. `/status` should show `whatsapp: connected`. If it shows LOGGED OUT,
   re-pair: the session did not survive the move (rare) — run a pairing flow.

## 6. Connect the Business chatbot

**[YOU]** On the phone: Settings → Telegram Business → Chatbots → pick the new
bot → enable for all private chats. `/status` shows `tg_business: connected`
after the first update arrives, and the bot messages you.

## 7. Smoke tests

- Email yourself "meeting tomorrow 15:00 with Dana" → confirm card → event.
- Message a whitelisted WhatsApp group with a concrete event → confirm card.
- Delete a message in a private TG chat → card in the log channel.
- `/costs` shows the day's LLM spend.
