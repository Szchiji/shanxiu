# Bot API outbound audit

`BOT_SEND_AUDIT` JSON records are metadata only. Installed in `create_app`, and
bound before main/clone initialization. PTB **21.11.1** `Bot._do_post` is the
shared request layer used by `Bot` and `ExtBot` (including their rate limiter's
actual callbacks). No payload mutation, new Telegram request, retry, DB write,
transaction, global HTTP hook, or content filtering is added.

## Coverage inventory

- All PTB `send*`, `copy*`, `forward*`, `edit*` methods passing through that layer:
  text/media/albums, copy/forward single and batches, edits including inline
  boolean responses. Also reactions, message deletion, stop live location/poll,
  and game scores. Business handlers, scheduler, coupons and channel buttons use
  these same calls; their content and dispatch conditions are unchanged.
- Current direct HTTP Bot API writes in `app/modules/core/routes.py`:
  `do_like` (`setMessageReaction`), `_set_chat_member_tag` (`setChatMemberTag`)
  use `bot_api_post`. `_telegram_get_chat_member` uses the same wrapper but, as
  a read-only method, emits no send record. The original Response is returned.
- Direct `getMe`/`getChat` calls and public t.me/media scraping are reads and not
  send-audited. `my_telegram.py` uses my.telegram.org, not Bot API sends.
- `webhook` and `clone_webhook` acknowledge with plain text, dispatching updates
  to PTB; neither sends a Bot API method in its HTTP response. An inventory test
  guards this current contract and direct requests write call sites.

## Record format and interpretation

Each request has a UUID `attempt_id` shared by an `attempt` and `result` event.
Time is ISO 8601 with **+08:00**. Fields are whitelisted: cached getMe numeric bot
ID, bound clone ID (null for main/unbound), API method, transport, nearest app
module/function, safe target/source chat ID, source message ID(s), returned
`messages` entries with target chat/message IDs, fixed status/error enums. Copy
responses lacking a chat use the request target; albums/batches preserve all
returned IDs (defensive cap 1000). Inline edits may return only a boolean: no
ordinary chat/message ID is available. An unattributed caller is explicit.
Startup `installed`, `binding`, and `identity_ready` events show installation
and main/clone binding; identity before existing getMe completes can be null.
Direct HTTP bot identity is emitted only if previously verified by existing
PTB getMe, never via an extra lookup. Unknown identity remains null.

`failed` means a definite Bot API rejection (e.g. BadRequest, Forbidden, 429).
`unknown` includes network errors, cancellation, unclassified errors or malformed
responses; the message **may have been delivered**. Pending attempts with no
result after a crash are also unresolved, not proof of failure. Audit code never
retries and rethrows the original error or returns the original result unchanged.
Audit/logger exceptions are contained. Installation failure is reported best
effort and never prevents bot startup.

No text/caption, hashes, media, URLs, button/callback/initData/session data,
files, token, exception text, request/response body, or full URL is recorded.
No per-send DB work or full stack dump. Only a bounded frame walk (max 32) and
standard logging; two small records per audited request, no sampling. Root log
level must include INFO. Existing centralized token redaction remains in place.

## Searching and retention

Use Railway runtime log search for `BOT_SEND_AUDIT`, optionally bot ID, chat ID,
message ID, `attempt_id`, `"phase":"result"`, or `"event":"identity_ready"`.
Check the embedded JSON `time` rather than assuming the platform's displayed
zone. Reconcile exact bot/chat/message IDs; timestamps alone are insufficient.

Records go to stdout/standard platform logging only. No new database, queue,
external sink or local audit file. Retention/volume limits are those of the
existing Railway plan; no permanent or lossless retention guarantee, and no
retention settings were changed. Logger failure/process death/platform dropping
records can create gaps. Only platform-access holders should inspect logs since
numeric chat IDs are still operational metadata.

## Explicit boundaries

This is forward-only observability, not prevention or proof of token misuse.
Cannot reconstruct the 2026-11-20 16:54 message. External callers using a token,
other deployments/processes without this code, Telethon/MTProto userbot traffic,
and future custom transports/raw HTTP bypasses do not pass through this hook.
Future new Bot API HTTP sends must use `bot_api_post`; an arbitrary dependency
could still bypass both audited interfaces. PTB upgrades require compatibility
review/tests because `_do_post` is a private API. Missing matching audit logs alone
cannot establish who sent a message or that a credential was compromised.
