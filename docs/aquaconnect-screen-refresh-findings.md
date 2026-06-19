# AquaConnect Screen Refresh — Research Findings

Resolved 2026-06-19. Answers to the handoff doc question of whether a
read-only status path exists that doesn't inject keypad events.

## Root Cause of the Wedge

Our sidecar was polling with `POST /WNewSt.htm` body `KeyId=00&` every 3
seconds. The firmware's GoAhead server scans the raw POST body for the
substring `KeyId=` to decide whether to call `WebsProcessKey()`. `KeyId=00`
contains that substring, so every "no-op read" was queued as a phantom keypad
event — **~29,000 times per day**. The keypad event queue or its associated
debounce/timer state is not designed for that volume and eventually wedges the
box. Reads stay live; writes are silently dropped. Sticky until power-cycle.

## The Fix

Change the poll body from `KeyId=00&` to `Update Local Server&`. Same URL,
same method, same response format. Nothing else changes.

## How the Native Web UI Works

Two completely separate functions in `WebsFuncs.js`, with **no shared code
path**:

### Screen refresh (pure read) — `ReqWebsData()`

```js
function ReqWebsData() {
    MyXmlRegObj.open("POST", "WNewSt.htm", true);
    MyXmlRegObj.onreadystatechange = function() { processWebsReqChange() };
    MyXmlRegObj.send("Update Local Server&");
}
setInterval(ReqWebsData, 300);
```

- Body: `Update Local Server&` — no `KeyId=` substring anywhere.
- Never touches `WebsProcessKey()`. Pure read, zero keypad-event side-effect.
- Runs every **300ms** natively (safe — the box has run this continuously since
  manufacture without wedging).

### Keypress handler — `WebsProcessKey()`

```js
function WebsProcessKey(KeyNum) {
    MyKeySendObj.open("POST", "WNewSt.htm", true);
    return;                              // bug: dead code after this
    MyKeySendObj.send("KeyId=" + KeyNum + "&");
}
```

- Separate XHR object (`MyKeySendObj`, not `MyXmlRegObj`).
- Body: `KeyId=NN&`. The trailing `&` is required or the key is silently
  dropped.
- Goes through `WebsProcessKey()`, queues a keypad event, triggers the 0.9s
  inter-event debounce.
- Note: browser-side button clicks are dead code (the stray `return;` before
  `send()`). Our raw socket implementation is the correct path for keypresses.

## Request Details

| | Screen refresh (read) | Keypress (write) |
|---|---|---|
| **Method** | POST | POST |
| **URL** | `/WNewSt.htm` | `/WNewSt.htm` |
| **Body** | `Update Local Server&` | `KeyId=NN&` |
| **Side-effect** | None | Keypad event queued |
| **Min gap required** | None observed | ~0.9s |
| **Response format** | Same (LCD + LED field) | Same |

## Response Format (confirmed live sample)

```
<body>
  Pool Temp  76&#176F   xxx
&nbsp;xxx
TECD4C333333xxx
</body>
```

- Lines 1-2: current LCD text (scrolls through temps, salt, chlorinator %, etc.)
- `&nbsp;` = blank second LCD line when panel shows only one line
- `TECD4C333333` = equipment-state nibble field (6 chars, same encoding we
  already parse)
- Each line terminated by literal `xxx`
- `&#176` = degree symbol (no trailing `;` — firmware HTML is slightly malformed
  but parsers tolerate it)

## Key Facts for Future Reference

- **Only one HTTP endpoint exists:** `/WNewSt.htm`. No XML/JSON/status sibling.
- **No documented rate limit.** 300ms native polling (from the box's own UI) is
  the best available evidence for safe read frequency.
- **`_post('00')` is NOT safe as a read.** It goes through `WebsProcessKey()`
  and is a keypad event. Use `_read()` (→ `Update Local Server&`) for all
  status polling and post-action confirm reads.
- **`_post('00')` IS correct for the wedge probe.** The probe intentionally
  exercises the `KeyId=` handler to verify the command path is alive.

## Implementation

`sidecarClient.py` now has:

- `_request(body)` — shared raw transport (builds the socket, enforces timing)
- `_post(key_code)` — wraps `_request(f'KeyId={key_code}&')`, use for keypresses
- `_read()` — wraps `_request('Update Local Server&')`, use for status reads

Poll loop and confirm-burst reads use `_read()`. The wedge probe's baseline
reads use `_post('00')` intentionally.
