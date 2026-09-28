# EmailConvo

An automated weekly email conversation between two Gmail accounts, powered
by Claude. Both agents generate and send real emails through Gmail; neither
human reads or replies. **Everything is controlled from `config.json` — no
Python code ever needs to be modified.**

---

## Table of contents

1. [Quick start](#quick-start)
2. [Configuration — the only file you need to edit](#configuration--the-only-file-you-need-to-edit)
3. [Running the demo](#running-the-demo)
4. [Weekly automation](#weekly-automation)
5. [Dry-run mode](#dry-run-mode)
6. [Remote Gmail authorization](#remote-gmail-authorization)
7. [Prompt templates](#prompt-templates)
8. [Switching Gmail accounts](#switching-gmail-accounts)
9. [Architecture](#architecture)
10. [Memory and conversation history](#memory-and-conversation-history)
11. [Security](#security)
12. [Troubleshooting](#troubleshooting)

---

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Add your Anthropic API key
cp .env.example .env
# open .env and fill in: ANTHROPIC_API_KEY=sk-ant-...

# 3. Place credentials.json (OAuth client from Google Cloud Console)

# 4. Run
python demo.py
```

On first run a browser opens for each Gmail account. Authorize each one and
the token is saved. Every run after that is fully automated.

---

## Configuration — the only file you need to edit

Open `config.json`. These are the five fields you'll change for every demo:

```json
{
  "weekly_topic": "Discuss this week's campground operations updates.",
  "subject": "Weekly Operations Check-in",
  "conversation_rounds": 2,

  "schedule": {
    "enabled": false,
    "day": "monday",
    "time": "09:00"
  }
}
```

| Field | What it does |
|---|---|
| `weekly_topic` | What the two agents discuss. Change this to change the conversation. |
| `subject` | The email subject line that appears in Gmail. |
| `conversation_rounds` | `1` = opener + one reply (2 emails). `2` = opener + reply + follow-up (3 emails). |
| `schedule.day` | Day of week the scheduler fires (`monday` – `sunday`). |
| `schedule.time` | Time of day in 24-hour format (`"09:00"`, `"14:30"`, etc.). |

**No Python code needs to be modified.** Save `config.json` and run `python demo.py`.

### Other config fields

```json
{
  "dry_run": false,
  "debug": false,
  "schedule": { "enabled": false }
}
```

| Field | What it does |
|---|---|
| `dry_run` | `true` = generate emails and print them without sending anything. Safe to run anywhere without OAuth. |
| `debug` | `true` = show verbose Gmail API logs (send headers, inbox search results). Leave `false` for clean demo output. |
| `schedule.enabled` | `true` = start the weekly cron job when running `python scheduler.py`. |
| `reply_delay_seconds` | Controls how long agents pause before replying. Makes the demo feel like a real email exchange. Set `enabled: false` to disable. Use `min: 5, max: 10` for quick testing. |

### Bot configuration

```json
{
  "bot_a": {
    "name": "Li",
    "email": "jiaheli0604@gmail.com",
    "role": "Operations Coordinator",
    "style": "clear, concise, professional",
    "template": "bot_a"
  },
  "bot_b": {
    "name": "Brian",
    "email": "testbotuno1@gmail.com",
    "role": "Manager",
    "style": "practical, direct, asks for next steps",
    "template": "bot_b"
  }
}
```

`name`, `role`, and `style` are injected into the Claude prompt. `template`
points to a file in `prompts/` — see [Prompt templates](#prompt-templates).
To swap Gmail accounts, change `email` and delete the matching token file.

---

## Running the demo

```bash
python demo.py
```

Shows a summary of what will happen, asks for confirmation if `dry_run` is
false, runs the conversation, prints the transcript, and saves it to
`transcripts/YYYY-MM-DD_HH-MM-slug.md`.

To use a preset demo config:

```bash
python demo.py --config demo/weekly_operations.json
python demo.py --config demo/maintenance_issue.json
python demo.py --config demo/marketing_update.json
python demo.py --config demo/budget_review.json
```

Each file in `demo/` is a self-contained config with `dry_run: true` and a
different scenario — safe to run for a presentation without sending real emails.

---

## Weekly automation

To have conversations run automatically every week:

**1. Enable the schedule in `config.json`:**

```json
{
  "schedule": {
    "enabled": true,
    "day": "monday",
    "time": "09:00"
  }
}
```

**2. Start the scheduler:**

```bash
python scheduler.py
```

The process runs indefinitely. Press `Ctrl+C` to stop. Config is reloaded on
every fire, so you can edit `weekly_topic` without restarting.

---

## Dry-run mode

Set `"dry_run": true` in `config.json` to generate and display messages
without sending any real email and without touching OAuth:

```json
{ "dry_run": true }
```

The full conversation loop runs end-to-end using an in-memory mailbox. Claude
generates real responses. Nothing leaves the process. Useful for verifying
topic, tone, and format before a live demo.

---

## Remote Gmail authorization

Use this when another person (e.g. Brian) needs to authorize their Gmail on
your machine. `oauth_onboard.py` starts a local web server; ngrok makes it
reachable from anywhere.

**1. Add Brian's Gmail as a Test User in Google Cloud Console**

Go to APIs & Services → OAuth consent screen → Test users → add Brian's address.

**2. Add the ngrok callback URL as an Authorized redirect URI**

In Google Cloud Console → Credentials → your OAuth client → Authorized redirect
URIs, add:

```
https://<your-ngrok-url>/oauth2callback
```

**3. Start the onboarding server**

```bash
python oauth_onboard.py
```

**4. Start ngrok in a separate terminal**

```bash
ngrok http 5000
```

Copy the `https://` URL from ngrok's output (e.g. `https://abc123.ngrok.io`).

**5. Restart the server with your ngrok URL**

```bash
BASE_URL=https://abc123.ngrok.io python oauth_onboard.py
```

**6. Send Brian the link**

Send Brian `https://abc123.ngrok.io`. He opens it in any browser.

**7. Brian clicks "Connect Bot B Gmail" and signs in**

Google's OAuth screen opens. Brian signs in with his Gmail account and grants
access. The success page confirms authorization.

**8. `token_bot_b.json` is saved locally**

The file appears in the project folder automatically. Brian can close the page.

**9. Run the demo**

```bash
python demo.py
# or, for weekly automation:
python scheduler.py
```

---

## Prompt templates

All AI persona, tone, rules, and output format instructions live in
`prompts/*.md` — not in Python. To change how the bots write, edit the
markdown file and run `python demo.py`. No code changes needed.

The default per-bot files are `prompts/bot_a.md` and `prompts/bot_b.md`.
Each file has comments at the top explaining every placeholder.

### Available placeholders

| Placeholder | Value |
|---|---|
| `$agent_name` | Bot's name from `config.json` |
| `$agent_role` | Bot's role from `config.json` |
| `$agent_style` | Bot's style from `config.json` |
| `$sender` | Sending bot's email address |
| `$receiver` | Recipient's email address |
| `$topic` | `weekly_topic` from `config.json` |
| `$memory_section` | Recent conversation history |
| `$conversation_section` | Messages exchanged so far this run |
| `$instruction` | "Write the opening message" or "Write the next reply" |

### Creating a new template

1. Copy `prompts/general.md` to `prompts/<name>.md`
2. Edit the markdown
3. Set `"template": "<name>"` in the bot's config block

---

## Switching Gmail accounts

**No code changes required.** Edit `config.json` and replace the token file.

1. Update `bot_a.email` (and/or `bot_b.email`) in `config.json`
2. Delete the matching token: `rm token_bot_a.json` (or `token_bot_b.json`)
3. Add the new address as a Test user in Google Cloud Console → OAuth consent screen → Test users
4. Run `python demo.py` — a browser opens once for the new account

---

## Architecture

```
EmailConvo/
│
├── config.json              ← Edit this to control everything
├── .env                     ← ANTHROPIC_API_KEY (not in git)
├── credentials.json         ← Google OAuth client secret (not in git)
├── token_bot_a.json         ← Bot A's Gmail token (not in git)
├── token_bot_b.json         ← Bot B's Gmail token (not in git)
│
├── demo.py                  ← Primary entry point
├── main.py                  ← run_conversation() engine
├── scheduler.py             ← Weekly cron automation
│
├── messaging_service.py     ← Transport interface (Message, MessagingService)
├── gmail_service.py         ← Gmail implementation
├── dry_run_service.py       ← In-memory simulation (no real email)
│
├── agents.py                ← EmailAgent: generate, send, poll inbox, reply
├── claude_service.py        ← Claude API wrapper
├── memory_service.py        ← Local JSON conversation history
├── console.py               ← Clean terminal display
│
├── prompts/                 ← Edit these to change how bots write
│   ├── bot_a.md             ← Bot A persona and rules
│   ├── bot_b.md             ← Bot B persona and rules
│   └── general.md           ← Shared fallback template
│
├── demo/                    ← Swap-ready demo configs
│   ├── weekly_operations.json
│   ├── maintenance_issue.json
│   ├── marketing_update.json
│   └── budget_review.json
│
├── transcripts/             ← Saved conversation transcripts (not in git)
└── history/                 ← Conversation memory (not in git)
```

### Conversation flow

```
demo.py / main.py
  │
  ├── Bot A generates opening message (Claude + bot_a.md prompt)
  ├── Bot A sends via Gmail
  │
  ├── Bot B polls Gmail inbox every 5s
  ├── Bot B reads Bot A's email from Gmail   ← Gmail is source of truth
  ├── Bot B generates reply (Claude + bot_b.md prompt)
  ├── Bot B sends reply via Gmail
  │
  └── (if conversation_rounds ≥ 2)
      ├── Bot A polls Gmail inbox
      ├── Bot A reads Bot B's reply from Gmail
      ├── Bot A generates follow-up
      └── Bot A sends follow-up
```

All replies carry RFC 2822 threading headers so Gmail groups the entire
conversation into one thread.

---

## Memory and conversation history

Every email is appended to `history/conversation_history.json`. Before each
Claude generation step, the 10 most recent records are loaded and included in
the prompt — so agents remember previous weeks and don't treat every check-in
as brand new. Records are deduplicated by Gmail message ID.

`history/` is in `.gitignore`.

---

## Security

| File | Rule |
|---|---|
| `credentials.json` | Never commit. Contains the OAuth client secret. |
| `token_bot_a.json`, `token_bot_b.json` | Never commit. Carry live Gmail access. |
| `.env` | Never commit. Contains the Anthropic API key. |
| `history/`, `transcripts/` | Never commit. Contain real email content. |

All are in `.gitignore`.

---

## Troubleshooting

**403 access_denied during OAuth**
Your Gmail address isn't in the Test users list. Go to Google Cloud Console →
APIs & Services → OAuth consent screen → Test users → add the address.

**`credentials.json` not found**
Download the OAuth 2.0 Desktop client JSON from Google Cloud Console and save
it as `credentials.json` in the project root.

**`ANTHROPIC_API_KEY not found`**
Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY=sk-ant-...`

**Logged into the wrong Gmail account during OAuth**
Delete `token_bot_a.json` (or `token_bot_b.json`) and re-run. Use "Use
another account" in the browser to select the correct address.

**Bot B times out waiting for Bot A's email**
Gmail can delay delivery by 30–60 seconds. The default timeout is 120 seconds.
Add `"inbox_timeout_seconds": 300` to `config.json` to wait longer.

**Scheduler fires but conversation fails**
Run `python demo.py` directly first to confirm the conversation works before
enabling `schedule.enabled`.
