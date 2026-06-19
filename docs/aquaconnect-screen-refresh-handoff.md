# Handoff: How does the AquaConnect web UI refresh its live "screen"?

## TL;DR — what I need from you

The Hayward **AquaConnect** web box (GoAhead "Webs" embedded HTTP server at
`192.168.50.100`) shows a **live, constantly-updating LCD "screen"** in its web
UI. I need to know **exactly how that screen gets refreshed** so I can copy the
mechanism. Specifically:

1. **The URL** the page requests repeatedly while the screen animates
   (path + any query string).
2. **The HTTP method** — `GET` or `POST`. *(This is the single most important
   answer.)*
3. **The request body / payload** — does it carry a `KeyId` parameter, or is it
   empty?
4. **The refresh interval** (roughly how many seconds between requests).
5. **The response format** — what the body looks like (paste a sample).
6. Whether the refresh is read-only or whether it goes through the firmware's
   keypress handler (`WebsProcessKey()`).

If you can give me a read-only request that returns the screen **without
registering a keypad event**, that solves the whole problem below.

---

## Why this matters (the problem I'm trying to solve)

I've built a Homebridge plugin + Python sidecar that mirrors the pool controller
into HomeKit. To keep HomeKit's state live (temps, salt, chlorinator %, which
circuits are on), the sidecar **polls the box every 3 seconds** to read the
current LCD frame + equipment-state bytes.

The way it polls today is:

```
POST /WNewSt.htm HTTP/1.1
Host: 192.168.50.100
User-Agent: curl/7.88.1
Accept: */*
Content-Type: application/x-www-form-urlencoded
Content-Length: 9

KeyId=00&
```

`KeyId=00` is meant as a "no-op read." **But on this firmware, `KeyId=00` is
processed as a keypad event** by `WebsProcessKey()` (found in `WebsFuncs.js`
around line 690 — the firmware scans the body for `KeyId=` up to a trailing
`&`). So our "read" is actually a **null button press**.

### The symptom

The box ran for **months** under normal human use (occasional button taps)
without trouble. Since we started polling, it **wedges every few hours**: HTTP
POSTs keep returning `200 OK`, the screen keeps updating, but **keypresses are
silently dropped** — the box stops relaying commands to the RS-485 panel until
it's **power-cycled**. (Reads stay live; only writes die.)

Our strong hypothesis: **we are injecting ~29,000 phantom keypad events per day**
(one every 3 seconds, 24/7), and the firmware's keypad/event queue eventually
gets into a stuck state it was never designed to reach. Human use never
generated anywhere near that event volume.

### The fix we're chasing

The web UI's screen updates **continuously** on its own — which means there is
almost certainly a **read-only refresh path** (a plain `GET` of some resource)
that returns the LCD frame **without** calling `WebsProcessKey()` / without
counting as a keypad event. If we switch our 3-second poll to *that* mechanism,
we stop injecting events entirely and (we expect) stop the wedging — while
keeping all our live data.

That's what this handoff is to find.

---

## What we already know about the box

- **Server:** GoAhead "Webs" embedded HTTP server. It is picky: it silently
  ignores requests with extra headers (e.g. `Accept-Encoding: identity`,
  `Connection:`, a Python user-agent). A lean curl-style header set works
  reliably. (We hand-build the raw request over a socket because of this.)
- **Keypress endpoint:** `POST /WNewSt.htm` with body `KeyId=NN&` (e.g.
  `KeyId=09&` = LIGHTS key). The trailing `&` is required or the key is dropped.
- **Response format of `WNewSt.htm`:** an HTML-ish body whose meaningful lines
  live inside `<body>…</body>`, are CRLF-separated, and each is terminated by a
  literal `xxx` marker. Example decoded lines:
  ```
  Thursday
  5:47P
  TECD4C333333
  ```
  - The first lines are the **LCD text** (what the panel screen shows; scrolls
    through Pool Temp / Air Temp / Salt Level / Chlorinator % / etc.).
  - The 6-char alphanumeric line (e.g. `TECD4C333333`) is the **equipment-state
    field**: each LED is one 4-bit nibble — `3`=absent, `4`=off, `5`=on,
    `6`=blink.
- **Timing:** the box ignores any key within ~0.5–1s of the previous event, and
  a `KeyId=00` read counts as one of those events. We currently enforce a 0.9s
  minimum gap between *all* requests.

---

## Concrete questions to answer

Please capture this either from **browser DevTools → Network** (easiest and most
authoritative) or by decomposing the page's JavaScript.

### From the browser (preferred)
1. Open the AquaConnect web UI, open **DevTools → Network**, and watch the
   request that **repeats** while the LCD screen animates.
2. For that repeating request, report:
   - Full **URL** (path + query string).
   - **Method** (GET/POST).
   - **Request payload** (empty? or `KeyId=…`?).
   - **Interval** between repeats (seconds).
   - A **sample response body** (paste it verbatim).
3. If the page uses framesets, note which `<frame>`/`<iframe>` is reloading and
   its `src`.

### From the JavaScript (if you have the files)
- Look in the page `<head>` scripts and in `WebsFuncs.js` for a
  `setInterval`/`setTimeout` that fires an `XMLHttpRequest` / `fetch` /
  `location.reload()` / frame reload on a timer. That is the refresh loop.
- Report the **URL and HTTP method** it requests, and the **interval**.
- Confirm whether the server-side handler for that URL calls `WebsProcessKey()`
  (i.e. whether it registers a keypad event) or is a pure read.

### The key yes/no I'm after
> Is there a request — ideally a plain `GET` (e.g. `GET /WNewSt.htm` with no
> body, or some sibling file like a `.xml` / `.cgi` / status `.htm`) — that
> returns the current LCD frame + equipment-state bytes **without** registering
> a keypad event?

If yes: give me the exact request (method, path, query, headers if they matter)
and a sample response. That's all I need to switch our poll over and (hopefully)
end the wedging.

---

## Bonus questions (nice to have, not required)

- Does the box expose **any** structured/status endpoint (XML/JSON) separate
  from the LCD-scrape, that reports temps / equipment state directly?
- Is there a documented **rate limit** or recommended poll interval for the web
  interface?
- Does the firmware have a known **watchdog** or event-queue depth that, once
  exceeded, drops writes until reboot? (That would confirm our "event flooding"
  theory.)
