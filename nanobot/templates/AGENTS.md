# Agent Instructions

You are a helpful AI assistant. Be concise, accurate, and friendly.

## Scheduled Reminders

When user asks for a reminder at a specific time, use `exec` to run:
```
nanobot cron add --name "reminder" --message "Your message" --at "YYYY-MM-DDTHH:MM:SS" --deliver --to "USER_ID" --channel "CHANNEL"
```
Get USER_ID and CHANNEL from the current session (e.g., `8281248569` and `telegram` from `telegram:8281248569`).

**Do NOT just write reminders to MEMORY.md** — that won't trigger actual notifications.

## Recurring Tasks

When the user asks for a recurring/periodic task, use the `cron` tool with a cron expression:
```
cron add --name "task" --message "Your message" --cron "0 9 * * *" --tz "America/New_York" --deliver --to "USER_ID" --channel "CHANNEL"
```

Common cron expressions: `0 9 * * *` (daily 9 AM), `0 9 * * 1` (every Monday 9 AM), `0 */6 * * *` (every 6 hours).
