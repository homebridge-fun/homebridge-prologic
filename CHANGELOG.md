# Changelog

All notable releases of `homebridge-prologic` (Homebridge plugin + Python
sidecar + web cockpit for a Hayward AquaPlus / ProLogic pool controller).

## Unreleased

### Fixed

- **A heater setpoint written with the panel's other degree glyph was read as
  no value at all.** The LCD's degree symbol reaches the sidecar as `°` on some
  frames and `_` on others — both encodings are in the captured corpus, one
  menu apart, on the same hardware. The shape ledger has folded the two since
  it was built, but `_norm` does not, so the *parsers* see the raw glyph, and
  both setpoint patterns accepted only `°`. An `85_F` frame therefore dropped a
  real setpoint on passive capture, and made a setpoint write's read-back look
  as though it had never landed. Both now accept either encoding via a shared
  `_DEG_F`.
- `harvest_frames.py --append` numbered placeholder names from 1 on every run,
  so a second harvest before the first was reviewed produced duplicate names
  and failed the corpus's uniqueness test instead of appending. Numbering now
  continues past what is already there.

### Changed

- **The captured frame corpus is reviewed.** All 74 frames now carry a real
  name and a recorded reason for their expectation, and are marked
  `reviewed: true` — closing the known limitation noted in 0.10.0. The
  expectations were a snapshot of parser output, which proves nothing on its
  own; the review is what turns them into assertions that the parser is
  *right*, including the majority whose correct answer is "this frame says
  nothing". That distinction is load-bearing: the blanked-value frames
  (`pool_chlorinator_blanked`, `spa_speed_blanked`) must stay empty rather than
  read as 0%, and `filter_timer_t1_speed_link` must not be mistaken for a live
  pump speed. A test now requires any entry marked reviewed to say why.

## 0.10.0 — Configurable lights, first test suite, frame-capture tooling

### Added

- **The light standard and relay are configurable per body.** Which light is
  on which circuit was hardcoded to one installation (pool = Hayward
  ColorLogic on `LIGHTS`, spa = Pentair IntelliBrite on `AUX_1`). The two
  standards select colours differently — ColorLogic steps relatively from the
  current colour, IntelliBrite counts absolutely from a reset — so a
  mismatched install landed every scene on the wrong colour, and two lights of
  the same standard could not be expressed at all. Set
  `poolLightType`/`poolLightCircuit` and `spaLightType`/`spaLightCircuit` in
  the Homebridge UI. Defaults are the previous values, so nothing changes
  unless you set them.
  **Not supported:** Hayward OmniDirect (networked ColorLogic on OmniLogic),
  which selects colours directly instead of by power cycling. Different
  platform, different protocol — not something configuration can reach.
- **The first automated tests — 138 of them**, plus CI running lint, build,
  a Python syntax check and documentation checks on every push.
- **Frame-capture tooling for the test corpus.** The sidecar now records every
  distinct LCD screen it has ever seen (`GET /display/shapes`), surviving both
  the 60-frame display ring and a restart, so a fault that appears at 3am is
  still there in the morning. `scripts/harvest_frames.py` turns that into test
  fixtures and reports which screens the parser does not understand.

### Fixed

- **The bridge-offline alert did not clear when the bridge came back.** The
  banner kept showing a resolved outage for the remainder of its ten-minute
  window, while claiming to be current — and its own text promised it
  self-cleared on reconnect.
- **The panel mirror reflowed whenever a field blinked.** At the panel a
  blinking value blanks in place; in the cockpit everything to its right
  shifted, which is disorienting exactly when navigating menus. The mirror now
  renders the raw 20x2 frame with column positions preserved.
- **The pad Pi's persistent journal never worked.** `install-pad.sh` set
  `Storage=persistent` in `journald.conf`, which Raspberry Pi OS overrides with
  a vendor drop-in forcing volatile storage — so every reboot wiped the crash
  logs the setting existed to keep. It now writes a drop-in that wins, and
  verifies the result instead of assuming it. The health log gained columns
  reporting whether the journal is persisting and what it costs on the SD card.

### Changed

- **`engines.node` is now `^22 || ^24`** — matching what Homebridge 2 itself
  requires. The previous `>=18.0.0` advertised versions the plugin cannot run
  on, and contradicted its own peer dependency.
- **No default AquaConnect address is shipped.** It previously defaulted to one
  installation's box IP, so a fresh install would silently talk to whatever
  device occupied that address on the user's network. The host must now be
  configured; an unset host is an error rather than a guess.
- **Super Chlorinate moved into the Chlorinator card** in the cockpit, below
  the pool and spa output controls, rather than sitting among the generic aux
  switches.
- `parse_ac_scroll` was extracted as a pure function, separating LCD parsing
  from state mutation. Behaviour-preserving; it makes the most bug-prone code
  in the project testable.

### Known limitations

- The captured frame corpus ships with its expectations unreviewed, so those
  tests currently assert that behaviour has not *changed* rather than that it
  is *correct*.
- Reading a value that only appears on the panel's idle scroll can lag by up to
  a minute. Sweeping the scroll on demand would fix that but is what used to
  lock up the AquaConnect box, so the delay is deliberate.

## 0.9.2 — CI, corrected Node support, dependency cleanup

- **Fixed: `engines.node` advertised Node versions the plugin cannot run
  on.** It declared `>=18.0.0`, but this plugin depends on Homebridge 2,
  which itself requires `^22 || ^24`. Node 18 and 20 were never viable —
  Homebridge 2 won't start on them — so the package was claiming support it
  couldn't deliver, and contradicting its own peer dependency. Now correctly
  `^22 || ^24`, which also matches Homebridge's verified-plugin requirement
  to support the current LTS releases.
- **Added CI** (`.github/workflows/ci.yml`) — the repo had none. Runs
  `npm ci` + lint + build on Node 22 and 24, plus a zero-dependency Python
  syntax check over `sidecar/`, on every branch push and pull request. The
  `lint` and `build` scripts already existed but nothing ran them
  automatically.
- **Upgraded ESLint 8 → 10** and migrated `.eslintrc.json` to flat config
  (`eslint.config.js`). ESLint 8 was end-of-life and warned on every
  install. Rules are ported 1:1, so this changes the config format, not what
  gets flagged. Dev-only — nothing here ships to users.
- **`npm audit` now reports 0 vulnerabilities**, down from 3 high. Two
  (`brace-expansion`, `js-yaml`) were dev-only transitive dependencies of
  ESLint 8 and cleared with that upgrade. The third was `axios`, whose
  lockfile pin moved 1.17.0 → 1.20.0; the declared range stays `^1.6.0`, so
  consumers are unaffected by the pin itself.
- **Added [`docs/testing-strategy.md`](docs/testing-strategy.md)** — a
  design sketch for a real test suite (what's cheaply testable here, what
  can only be verified against live hardware, and a suggested order),
  written ahead of opening the project to outside users. Nothing
  implemented yet beyond the CI above.

## 0.9.1 — 1.0-prep docs pass, npm publish readiness

- **README rewritten** for clarity/accuracy ahead of a wider release: a new
  Mermaid architecture diagram (plugin/sidecar/cockpit/pad Pi/RS-485 bridge),
  corrected install instructions (the plugin isn't on npm yet — from-source
  steps instead of the old broken `npm install -g` instructions), an accurate
  HomeKit accessory table, a new section documenting the web cockpit, and a
  new "For contributors" section linking to the deeper spec docs.
- **Added `LICENSE`** (MIT) — referenced by the README/package.json but
  didn't exist as a file before.
- **`docs/plugin-spec.md` and `docs/aqualogic-automation-spec.md` reviewed**
  for accuracy: fixed a broken example config (nonexistent `rs485Host`/
  `rs485Port` fields), a stale/contradictory note on which RS-485 frame type
  the bridge actually sends, HomeKit accessory tiles documented as shipping
  when they'd since been removed (VSP slot tiles — that control moved to the
  cockpit), a stale version header, and several smaller staleness/clarity
  fixes.
- **Fixed a real bug:** `deploy/deploy.sh` (personal one-command deploy
  script) hardcoded a since-deleted git branch and would have failed on next
  use; also cleaned up the same stale branch reference from `deploy/README-PAD.md`
  and `deploy/install-pad.sh`.
- **npm publish readiness:** `package.json` now has `author`, `repository`,
  `bugs`, `homepage`, and `displayName`, and a `files` allowlist so the
  published tarball only ships what installers need (trimmed from ~700KB to
  ~525KB unpacked — deploy scripts and dev-only sidecar debug tools no longer
  ship over npm, still available via git).

## 0.9.0 — Active Heat tile name fixed; per-body thermostat dead code removed

- **Fixed: the "Active Heat" thermostat tile's name could get permanently
  stuck showing the wrong body** (e.g. "Spa" after switching back to pool).
  Root-caused all the way through: dynamically swapping the name on every
  mode change didn't work, and neither did switching to a constant name while
  still pushing it every poll — `ConfiguredName` is a HomeKit characteristic
  the Home app treats as **user-owned** (edited by the person, not the
  accessory), and a pushed value can get stuck showing something with no
  relation to what's actually being sent. The fix that worked: the plugin now
  sets the name **once** at registration and never touches it again. If you
  want a different label, rename the tile yourself in the Home app — the
  temperature/setpoint values, which do update reliably, are what convey
  which body is active.
- **Removed dead code:** `ThermostatAccessory` used to support dedicated
  always-pool / always-spa tiles (`body: 'pool'|'spa'`) left over from an
  earlier three-accessory design that was removed from config long ago but
  never fully removed from the class. Only the single mode-following tile has
  shipped for some time; the class no longer has any code path that could
  rebuild the old per-body tiles.
- **Docs corrected** to match: a few places (including a broken example in the
  README's sample config) still described the removed per-body thermostats as
  a current feature. Also cleaned up several stale backlog references to code
  removed earlier this cycle (the legacy `rs485` backend, an old monkeypatch).

## 0.8.9 — Chlorinator/VSP-slot/spa-speed writes now record what actually landed

- **Fixed: hitting a hardware floor/ceiling on chlorinator %, a VSP filter
  slot, or spa speed silently recorded the requested value instead of the
  clamped one it actually reached.** Same underlying pattern as the heater
  setpoint fix, found via an audit of every setting-write for the same class
  of bug.
- **Fixed a separate bug in the same code: setting the SPA chlorinator %
  overwrote the POOL's cached percentage instead of the spa's** — the write
  always targeted the pool field regardless of which body was requested.

## 0.8.8 — Super Chlorinate now reverts immediately on a failed toggle

- **Fixed: an unconfirmed Super Chlorinate toggle reported success anyway.**
  `/superchlorinate` always returned 200 even when the write never actually
  confirmed on the panel, so HomeKit's optimistic tile had no failure to react
  to and could only self-correct on the next status poll instead of reverting
  right away. Now returns a proper failure when the confirmed state doesn't
  match the request, matching every other circuit's behavior.

## 0.8.7 — Cockpit shows the Super Chlorinate countdown

- **The cockpit's Super Chlorinate row now shows its live countdown** —
  `On · HH:MM left` instead of a bare "On" — using the same passive idle-scroll
  read that drives its on/off state. Clears when it turns off (by timeout or a
  toggle). Also exposed via `/status` as `super_chlor_remaining`.

## 0.8.6 — Super Chlorinate OFF fixed for real (while counting down)

- **Fixed: turning Super Chlorinate off while it was actively counting down
  silently did nothing** — the panel's Settings-menu item shows the live
  `HH:MM remaining` countdown while running, which 0.8.3's on/off detection
  didn't recognize as "on", so the OFF command matched (wrongly) and no press
  was ever sent. Also hardened against a dropped PLUS/MINUS press being
  misread as landed (the countdown's own per-second tick can fool a
  single-shot "did the text change" check) — it now re-verifies the actual
  on/off state and retries until confirmed, instead of trusting the request.

## 0.8.5 — Fixed a false "setpoint failed" alert on every heater temp change

- **Fixed: every heater setpoint write threw a KeyError right after succeeding,**
  surfacing a scary cockpit alert (`debounced write spa=93 failed: 'was_off'`)
  even though the temperature change had already landed on the panel. A
  long-standing (pre-dates this session) copy-paste bug: the setpoint write's
  success-log line read a `was_off` field that only exists on the *heater
  enable/disable* function's return value, not the *setpoint* function's. Fixed
  to log the fields that actually exist. No functional impact on the write
  itself — this was purely a false alarm in the log/alert path.

## 0.8.4 — Super Chlorinate state is now live (supersedes 0.8.3's caveat)

- **Super Chlorinate on/off is now passively tracked from the panel.** 0.8.3
  noted it wasn't read from the panel and would show Off until the first
  toggle — that's no longer true. The panel shows `Super Chlorinate HH:MM
  remaining` on its idle scroll while running (it runs a 24h countdown, then
  switches itself off automatically); the sidecar now watches for that frame on
  every backend and reflects it — including catching the automatic 24h off and
  a physical toggle at the panel, neither of which the sidecar could see
  before. State expires (flips off) if the countdown hasn't been seen in ~2.5
  minutes.

## 0.8.3 — Super Chlorinate OFF fix; added to the cockpit

- **Fixed: Super Chlorinate OFF was a silent no-op.** The on/off detector matched
  HTML markup (`>On<`) that never reaches the code (tags are stripped upstream),
  so it always read "off" — turning it ON worked by coincidence, but turning it
  OFF sent no key at all while still reporting success, leaving it on at the
  panel. Fixed to match the actual plain text.
- **Super Chlorinate now has a toggle in the cockpit** (the "Other" card), for
  parity with the existing HomeKit switch. Known caveat: its state isn't
  passively read from the panel, so it shows Off until the first toggle after a
  restart (same as the HomeKit switch already behaves).

## 0.8.2 — Circuit toggles no longer bounce

- **Filter / Lights / Aux / Spillover / Pool-Spa toggles stick.** Same race the
  heater had (0.8.1): the sidecar confirmed each toggle with a single instant
  read (~57ms after the press), before the panel's ~200ms LED broadcast reflected
  the change, so it returned a false 502 and the plugin reverted the tile (the
  bounce). Now it polls the circuit state / valve_mode for up to ~2-3s until it
  matches the target before confirming. The body-mode switch also waits for each
  press to register before pressing again (avoids overshoot). Note: this is a
  distinct fix from the 0.7.1 "No Response" (onSet timeout) fire-and-forget change
  — that fire-and-forget is what turned the false 502 into a visible bounce.

## 0.8.1 — Heater enable/disable no longer bounces

- **Heater toggle sticks on the first try.** Two fixes: (1) derive the heater's
  `enabled` (armed/Auto) state from the live `HEATER_AUTO_MODE` bit
  (`circuits['HEATER_1']`) instead of inferring it from the firing relay — the old
  inference forced "enabled" back on while the relay cooled down and broke the
  disable confirmation; (2) poll that armed bit for up to ~3s until it settles
  before confirming, instead of a single instant read that raced the panel's
  broadcast and falsely returned 502 (which bounced the HomeKit tile).

## 0.8.0 — Host sidecar installer; docs/spec sync for publishing

- **New host sidecar installer** (`sidecar/install.sh`) for a clean fresh setup:
  `--backend aquaconnect --aquaconnect-host <ip>` or `--backend rs485bridge
  --rs485bridge-host <pad-ip>` (optional `--rs485bridge-token`). Creates the venv,
  installs Flask, copies `pool_service.py` + the web cockpit, and registers the
  `pool-sidecar` systemd unit with a backend-correct `ExecStart`. `--dry-run`
  previews the unit. Replaces the removed legacy USR-W610 installer.
- **`requirements.txt` trimmed to Flask** — the host no longer needs `aqualogic`
  (RS-485 decode lives on the pad bridge since 0.7.2).
- **Docs/spec sync** — README quickstart shows the real install commands for both
  backends; spec file-tree and the rs485bridge backend status updated to reflect
  production reality.

## 0.7.3 — Backend-aware bridge health; spec sync

- **Bridge-health tile is backend-aware.** On the RS-485 pad bridge a "wedge" is
  just transient unreachability that self-heals, so the tile now reads **"Bridge
  Offline"** (Model "RS-485 pad bridge") with "pad unreachable — self-heals on
  reconnect (check pad Wi-Fi)" messaging, instead of AquaConnect's "Bridge Needs
  Rebooting / power-cycle the box".
- **Specs synced** with the 0.7.2 backend removal — `plugin-spec.md` and
  `aqualogic-automation-spec.md` now describe only `aquaconnect` (default) and
  `rs485bridge`, with the legacy `rs485` API surface removed from the docs.

## 0.7.2 — Remove the dead legacy RS-485 backend; docs accuracy

**Cleanup**
- Removed the never-working legacy `rs485` backend (raw frames over a WiFi/eth
  transparent serial bridge — the panel's post-keep-alive accept window can't be
  hit that way, so writes were dropped). ~780 lines gone from the sidecar: the
  keep-alive `KEY_*` machinery, `RealPanel`, the RS-485 observer/`panel_thread`,
  and the rs485-only debug/benchmark routes. The supported backends are
  **AquaConnect** and the **Raspberry Pi RS-485 pad bridge** (`rs485bridge`); the
  `--backend` default is now `aquaconnect`. No change to `/status` or behavior.

**Docs**
- README/description corrected: both connection modes, AquaConnect not disabled
  (RS-485 pad bridge is a hedge), deep automation (setpoints, light scenes,
  chlorinator, VSP) vs. earlier plugins' on/off — and the stale "not supported"
  limitations rewritten. Acknowledgments added (cupshir, swilson/aqualogic,
  SteveTheGeekHA).

## 0.7.1 — HomeKit responsiveness, unified Lights UI, honest temp history

**HomeKit "No Response" fixes**
- Every slow `onSet` handler that navigates the Settings menu (~15s) — the
  Heater Auto switch, the thermostat Heat/Off dial, Super Chlorinate, the light
  TV power + scene, and the bridge-test button — is now **fire-and-forget** so it
  returns immediately instead of timing out and showing "No Response". State
  reconciles in the background (reverts on failure).

**Lights — one unified cockpit section**
- Pool and Spa lights share a single **Lights** card: label + power pill, then a
  scene dropdown that **shows the current program** and a settings gear.
- **Smart scene pick:** a different scene sets it; the current scene while off
  just powers on; the current scene while on is ignored.
- Scene position now **persists to `backend.json`** across restarts.
- Visual polish: flat monochrome gear, dropdown chevron, muted text, larger
  touch targets, fonts aligned with At a glance.

**Temperature history**
- A stale feed (pad/box unreachable) now records an **honest gap** instead of a
  frozen last-value flatline; the pool/water temp is dropped while the spa is
  active (shared sensor). Adds `clean_temp_history.py` to retroactively gap past
  outage flats.

**Docs**
- Standard deploy is one block ending in the Homebridge restart.

## 0.7.0 — Lights that actually work, heater clarity, cockpit polish

Resolves pool/spa light scene selection on real hardware, cleans up the heater
readout, and slims the cockpit light settings.

**Pool light (Hayward ColorLogic)**
- **Corrected to the real light.** It's the **12-program ColorLogic (CL 4.0)**,
  not the 17-program Universal ColorLogic we'd assumed — confirmed against the
  user's manual and the hardware (one green, not two). Program list, dropdown,
  and HomeKit tile now match.
- **Reliable absolute selection via a resync anchor.** A single 11–14 s off
  re-synchronizes the light to program 1 (Voodoo Lounge) — hardware-confirmed.
  Scene selection steps the **minimum** number of off/on cycles from the tracked
  position, or anchors + steps when the position is unknown.
- **One-tap "Resync colors"** button in the pool light settings; a current-program
  badge shows on the card header.

**Spa light (Pentair IntelliBrite)**
- Absolute count with the `offset=+1` calibration **baked into the sidecar** (no
  longer a UI knob).

**Heater**
- **Enabled inferred from the relay.** A firing relay means the heater is armed,
  so the mode can never read "Off" while it's actually running — fixes the
  contradictory cockpit state. Cockpit labels are now **Heater: Enabled / Off**
  and **Heating now: Running / Idle**.

**Cockpit**
- Light settings are **body-aware and slimmed**: pool shows Resync + timing;
  spa shows timing + reset ms. Removed the UCL reset, manual toggle, set-current,
  step, and raw-count tools. Press feedback on every settings button.

**Docs**
- `colorlogic-research.md` rewritten with one self-contained section per light
  type and a "how we know it works" verification status; the superseded UCL
  theory is moved to an appendix.

## 0.6.0 — Heater clarity, pad hardening, observability

Refines the heater UX, makes the pad Pi freeze-proof, and adds the tooling to
diagnose anything that does slip through.

**Heater controls**
- **One mode-following thermostat.** Removed the dedicated pool/spa heater
  thermostats — one physical `HEATER_1` rendered as three tiles could disagree
  on heating/standby and on which setpoint was live. The config options
  (`enablePoolHeaterThermostat` / `enableSpaHeaterThermostat`) are gone from the
  schema so they can't be re-added; the single "Active Heat" tile mirrors the
  active body. Stale tiles are unregistered on startup.
- **Armed vs firing, separated.** The thermostat's `CurrentHeatingCoolingState`
  now reflects the actual relay-firing signal (`heater_active`), not the armed
  Auto-mode bit — so Heating/Idle means *firing*, and Auto/Off means *armed*.
  The web cockpit mirrors the split: "Heater mode" (Auto/Off) and "Heating now"
  (Running/Idle), plus per-body Auto/Off + Heating/Idle badges in the Heat card.
- **Correct valve mode.** `valve_mode` is derived from the live POOL/SPA LED
  bits instead of parsed LCD text, so it flips instantly.

**Pad Pi reliability (512 MB Pi Zero 2 W)**
- **Memory-pressure hardening**, folded into `install-pad.sh`: `earlyoom` (kills
  a hog before a swap-thrash freeze), `vm.swappiness=10`, persistent+capped
  journald, and a `MemoryMax` cap on the `pool-bridge` service. Traced three
  field freezes to chronic low headroom the hardware watchdog couldn't catch.
- **Clean re-image path.** `install-pad.sh` now bootstraps `python3-pip` (absent
  on Pi OS Lite) and the FTDI udev rule applies `latency_timer=1` on a live
  trigger (was `ACTION==add`-only, so it silently no-op'd until a reboot).
- **30-day health sampler.** `pool-healthlog.timer` logs memory/swap, load, Pi
  under-voltage/throttle flags, SoC temp, and daemon RSS to a rotating CSV — so
  an intermittent freeze or a power/brownout issue is diagnosable after the fact.

**Wedge + alerts**
- **Backend-aware wedge.** Messages no longer say "power-cycle the AquaConnect
  box" on the RS-485 bridge; the flag now **auto-clears** once the pad daemon is
  reachable again (the AquaConnect-gated recovery probe never cleared it before).
- **Coalesced alerts.** Repeated warnings collapse into one cockpit row with a
  `×N` count instead of spamming three per menu pass.
- Fixed a blank cockpit Panel Display on the rs485bridge backend (LCD stream hub
  name mismatch).

**Experimental (banked)**
- ColorLogic light-scene control in the sidecar (named scenes, select-by-name
  API, cockpit card, `/program` absolute select) plus full Hayward/Pentair
  research — parked as experimental pending night testing.

## 0.5.0 — RS-485 smart bridge in production

The big one: menu control moved off the AquaConnect HTTP box onto a **direct
serial link**, ~2.5× faster and 100% reliable.

- **Pad-Pi smart bridge.** A thin daemon (`sidecar/rs485_bridge.py`, systemd
  `pool-bridge`) on a pool-side Pi Zero 2 W owns the USB-RS485 serial link and
  its timing, exposing a small HTTP API (`/state`, `/key`, `/keys`,
  `/health`). The hop's sidecar drives it over Tailscale.
- **100% keypress landing.** Root-caused the residual ~33% drop to the **FTDI
  USB `latency_timer` (16 ms default)**; pinned to 1 ms via a udev rule → 30/30
  and 20/20 presses land vs the old TCP bridge's ~60% single-shot. `aqualogic`
  3.4's `_write_to_serial` `.send()`→`.write()` bug patched; `LONG_DISPLAY_UPDATE`
  frames captured so menu-nav LCD works.
- **`rs485bridge` sidecar backend.** `RS485BridgeBackend` mirrors
  `AquaConnectBackend` (`lcd` + `send_nav_key`) so the existing `MenuNavigator`,
  circuit-set, and prefetch paths work unchanged. **~2.5× faster** menu sweeps
  than AquaConnect (15.9 s vs 39.4 s), no wedging. Cut over to production.
- **Plugin backend selection.** `rs485bridge` selectable in the Homebridge UI
  (`rs485bridgeHost`/`rs485bridgePort`); AquaConnect kept as fallback.
- **Security / deploy.** Daemon binds the tailnet IP; optional bearer token
  (`RS485_BRIDGE_TOKEN`) — no secrets in git. One-command re-image installer
  (`deploy/install-pad.sh`) + runbook (`deploy/README-PAD.md`).
- **Cleanup.** Removed the Cloudflare-Tunnel-to-pad plumbing (Tailscale is the
  path) and the legacy `rs485` TCP-bridge backend from the plugin UI/config.
  Heater switch ⇄ thermostat sync; pump/VSP speeds removed from HomeKit
  (cockpit-only). Fixed a blank cockpit Panel Display on `rs485bridge` (LCD
  stream hub name mismatch).

## 0.4.2

- Wedge auto-re-arm; cockpit alerts pane.
- Heater write-confirm (immediate re-read after a heater toggle).
- Removed the legacy ContactSensor wedge form (now a Switch).
- Temperature-history chart with two-tier retention (5-min ≤1 day, 15-min
  beyond, 90-day cap).
- Security/robustness: HTML-escaping of fault/label text (XSS), and a
  FILTER-off-during-heater-cooldown "cry wolf" guard (no false wedge report).

## 0.4.0

- Version numbering realigned to the owner's scheme (the prior batch was
  effectively 0.3). No functional change beyond the version bump.

## 0.3.0

- **Interactive web cockpit** — control circuits, heater, chlorinator, VSP, and
  navigate the panel from the browser; live LCD stream (SSE).
- **Temperature history chart** in the cockpit.
- **Fault detection** — passive frame capture surfaces panel alerts; discovery
  log for unrecognized alert frames.
- **Caddy + HTTP Basic auth** for LAN cockpit access (`deploy/Caddyfile`,
  `deploy/set-cockpit-password.sh`); sidecar stays localhost-bound.
- Wedge/startup improvements.

## 0.2.1

- Cockpit and sidecar reliability/UX improvements.

## 0.2.0

Heater clarity, sensor accuracy, write-path safety, faster navigation.

- **Heater two-switch model** — "Heater Auto" (tappable, armed/Auto state) +
  "Heater Running" (read-only relay-firing), replacing the unreliable
  three-state fan.
- **Salt sensor** reads the real value (raised `VOCDensity` max 1000→4000 so the
  ~3200 PPM reading isn't clamped).
- **VSP speed slot tiles** — startup pre-fetch, debounced writes, zero-guard,
  per-slot floor (`vspSlotMinPct`). *(These pump/VSP HomeKit tiles were later
  removed in 0.5.0 — VSP is cockpit-only now.)*
- **Chlorinator %** writable (valve-mode aware, 600 ms debounce, snaps to panel
  grid).
- **Thermostat** setpoint debounced 600 ms (was a menu-nav per 0.5 °C step).
- **Frame-reader navigation** — N+1 requests per N keypresses (was N×5);
  inter-request gap lowered 0.9 s → 0.6 s.
- Wedge-risk audit of every slider-driven write path.

See `docs/RELEASE-v0.2.0.md` for the full 0.2.0 detail.

## 0.1.0

First tagged release; running on hardware (Hayward AquaPlus PS-8).

- **AquaConnect HTTP backend** alongside RS-485, selectable from plugin config
  (persisted to `backend.json`).
- Poll body switched `KeyId=00&` → `Update Local Server&`, eliminating ~29k
  phantom keypad events/day that caused the wedge condition.
- **Bridge wedge detection** — active AUX2 canary probe + a HomeKit tile that
  highlights when wedged and runs a live test on tap; immediate probe on any
  write failure.
- Heater enable/disable via Settings-menu nav with immediate confirm read;
  HEATER_1 switch + thermostats driven from one state field (no lag).
- Thermostats (pool/spa dedicated + mode-following auto), temperature sensors,
  chlorinator fan, VSP pump fan, spa-mode switch.
- Hourly rotating debug log + wedge-test harness.
