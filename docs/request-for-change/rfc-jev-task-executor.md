---
title: Jev task executor — closed browser and desktop subtasks without an LLM in the loop
status: draft
author: Ray Xu
created: 2026-09-28
last-audited: 2026-09-28
audited-at: 699083f906
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Jev task executor — closed browser and desktop subtasks without an LLM in the loop

- Feature preview; default off
- Related: [`rfc-wake-judge`](rfc-wake-judge.md) §3.1, which also needs the
  `noul` and `score` wire types this design adds.
- Prior art: [JevOnly](https://github.com/buluoray/JevOnly), a standalone harness
  that drives a browser and a desktop app with Jev as the only model.

Status: draft. Nothing of this design is on main. Code references were read at
`699083f906`.

## 1. Problem

When the agent needs a web page or a desktop app, every step goes through the
chat model. It reads a snapshot, often several thousand tokens of element tree,
picks one ref, calls `browser` or `computer_click`, and reads the next snapshot.
A ten-step errand such as "open the second search result and note its price and
rating" costs ten full model turns. Most of those turns involve no reasoning.
The model is picking one line out of a list the harness already wrote.

Kiro Crew already has a cheap model for that kind of question. The decisions
seam (`src/kiro_crew/decisions/`, spec
[`decisions.md`](../system-specs/modules/decisions.md)) sends Jev a closed menu
and gets back one pick with a calibrated probability. Seven points use it today
(`skills.select`, `message.steer`, `model.route`, `memory.recall`, `tool.risk`,
`compaction.keep`, `nudge.wake`), and none of them touches the browser or
Computer Use. Each point answers one decision inside a turn. None of them runs a
loop of twenty steps that observes, acts, checks the result and undoes a wrong
move.

JevOnly shows the loop works on its own. On its regression set (Google Flights,
Wikipedia, Hacker News, GitHub, two online-store tasks), seven of eight tasks pass on
every run; the eighth is stopped by a bot wall. A typical task takes about 20
steps, 80 Jev questions and 30 seconds, and costs about one cent. Jev never
writes text: every word the loop types comes from the task, a given fact or the
page.

## 2. Idea

Give the chat agent one tool per surface that hands a closed subtask to a Jev
loop and returns what the loop read:

```
jev_browser_task(goal, start_url, facts?, max_steps?)  -> {success, answer, values, stopped, trace_id}
jev_computer_task(goal, app, facts?, max_steps?)       -> {success, answer, values, stopped, trace_id}
```

The chat model still plans, decides when a subtask is closed enough to hand
over, and reads the result. The loop does the clicking. When Jev cannot finish,
the loop says so (`dead_end`, `max_steps`, `budget`, `needs_human`, or
`not_permitted` when the gateway refused to ask Jev at all) and the agent
carries on with its ordinary tools.

The loop is JevOnly's, shipped as an **external app** through the registry
rather than built in. Core gains four things: the wire types the loop asks in,
a consent scope per surface for the text that leaves the machine, an app-callable
route that asks Jev on the app's behalf under a gateway-held task lease, and a
structured Computer Use entry with its own sanitized output type.

## 3. Design

### 3.1 Placement: an external app, a small core surface

The loop, its questions, its snapshot code and its QA mode live in the app, and
core does not import them. This follows
[`rfc-everything-is-an-app`](rfc-everything-is-an-app.md) and the
`no-new-builtin-apps` rule in `AUTOSDE.yaml`. It also keeps the fast-moving part
out of the release train: the harness rules change with every new site the loop
meets.

The trust boundaries stay in core. That means the Jev credential, consent, the
governance ceiling, the Computer Use keystone and its dispatch checks, and the
SEL audit. The app contributes the two MCP tools and a skill that tells the
agent when to delegate. A subtask qualifies when it names its end state and the
values to read, and when no step needs judgement the goal does not already
carry.

### 3.2 Wire types

The loop asks `noul` questions (is this page off the path, did the last move
work, is this line the value asked for) and `score` questions (QA thresholds)
as well as `Choice`. `decisions/types.py` declares `Choice` alone, and
`impl_jev._to_wire` refuses anything else. `rfc-wake-judge` §3.1 names the same
gap but assigns it to a PR whose rollout entry does not list it, so this RFC
owns the widening. It adds `Noul` and `Score` dataclasses, their wire mapping
in `impl_jev.py` and the LLM lane, and tests for a malformed or partial answer.
The fail-closed domain check is `gate._answers_are_valid`, which today requires
a string value drawn from `question.options` and runs outside `decide`'s
`try`. Each new type therefore gets its own branch there: a `Noul` answer is a
probability in [0, 1] and a `Score` answer a number inside its declared range,
and a type with no `options` never reaches the `Choice` branch. A malformed
answer of either type yields `None`, never an exception. The wake judge can use
them as soon as they land.

### 3.3 Consent: one scope per surface

Browser page text and desktop control text are new kinds of egress. Today the
seam sends message excerpts, skill descriptions and, under `tool_args`, tool
arguments. It has never sent the text of a page or the value of a field in a
desktop app, and a desktop field can be more sensitive than a public page. So
the keystone gains two scopes, `browser_text` and `desktop_text`, placed exactly
the way `tool_args`, `memory_text` and `nudge_evidence` are placed. Each has a
strict reader (only a literal `true` consents, and absent means no), its own
`KEEP_*` sentinel in the locked read-modify-write, a `POINT_SCOPE_KEYS` entry,
an SEL verb and a row on the Decisions card. A consent recorded before the
scopes existed stays inert for both points, and disabling consent clears both.

Each scope is a standing grant that lasts until the owner revokes it. A task
lease (§3.4) bounds how long one task runs and how many questions it asks, but
it does not narrow the grant, and nothing ties a lease to a delegation the
agent made (§3.4). The card says this plainly: while the switch is on, any task
the enabled app runs, whether an agent delegated it or not, sends visible page
text, control labels and field values to the configured endpoint. The copy lives in the i18n
catalog. `browser.task` and `computer.task` are decisions point names. They are
not `computer_use.*` governance scopes, and Computer Use stays ungoverned as
AGENTS.md requires.

### 3.4 Asking Jev: an app-token route with a task lease

The app never holds the Jev key. The existing decisions routes are
dashboard-owner-only and refuse app callers
(`dashboard/handlers/decisions.py`), so the executor gets its own routes under
`/api/decisions/task-executor/`, reachable only by an app that declares that
prefix in `permissions.api`. The handler requires a verified app claim, checks
that this exact app is installed and enabled, and maps the route to the point
itself: `/browser/*` asks as `browser.task` and `/desktop/*` as `computer.task`.
The body cannot name an arbitrary point.

A task has three calls:

- `start` returns an opaque, gateway-minted lease. It carries the task id, app
  id, surface, an absolute expiry, a maximum number of questions and a maximum
  number of concurrent requests. The gateway picks the limits. The caller's
  `max_steps` only lowers them.
- `ask` requires the lease, consumes its counters atomically and answers
  through `decisions.gate` as one bounded batch per step, the way
  `compaction.keep` already batches. Every call re-checks consent, the endpoint
  binding, the surface's scope, sampling, the scrub and the fleet capability.
  The per-request timeout stays whatever `decisions.provider.timeout_ms` says.
- `finish` closes the lease. A closed or expired lease is refused, so a replay
  is refused too.

`decisions.gate.decide` reports every refusal (scope not granted, endpoint
unbound, sampling, ceiling) as a bare `None`, the same value a failed request
returns. The route does not pass that ambiguity on: it answers a refused `ask`
with a typed `refused` reason separate from a provider error. The loop ends
the task with `not_permitted`, and the task-summary row records the reason, so
"the scope is not granted" never reads as "Jev could not do it".

Nothing binds a lease to the MCP invocation the agent made, because no
caller-proof seam exists today between an app's MCP tool call and its backend's
HTTP request. The lease is therefore the app's own accounting. Above it the
gateway enforces an app-wide ceiling on live leases and on questions per
minute, so an app that opens a new task to reset its counters still hits a
fixed limit.

Decision logging stays best-effort, as `decisions.md` §5 specifies. A cheap
refusal writes nothing, and the day-file cap can stop appends. What the owner
can count on is one task-summary row per lease: task id, surface, steps,
questions, stop reason, elapsed time, and whether the app kept a detailed
trace. The per-step trace belongs to the app. `trace_id` is an opaque id the
app resolves in its own storage, never a filesystem path or a log offset. The
app's manifest must state retention, a byte cap and who can read it.
`decisions.outcomes` is not used, since it holds one receipt per point per turn.

### 3.5 Computer Use: one executor, two output adapters

`computer_use/tools.py` has one ordered chokepoint, `_dispatch`, reached through
`dispatch_tool` and its async wrapper `dispatch`. It validates the call,
resolves the target's OS identity, runs the enable check, the audit gate, the
target policy and the SEL audit, and then enters `_run`. `_run` performs the
action, settles, snapshots again and renders the result. Rendering carries a
security role, not only a display one. `render._render_record` drops a secure
element's title, value, actions, traits and frame. The raw records keep some of
those: on macOS a secure `ElementRec` blanks its value but keeps its title and
frame (`snapshot_macos.py`). Handing out raw `Snapshot` or `ElementRec` records
would therefore widen what leaves the governed module.

The change instead splits `_run` into one internal executor returning a private
`DispatchResult` and two adapters over it. `dispatch_tool` renders text as it
does today. `dispatch_structured` returns a new export type, never the raw
records, and applies the same output boundary the text path applies:

- A secure element keeps only its index, role, subrole and `secure: true`. Its
  title, value, actions, traits, focus and frame are dropped.
- Selected text, screenshot bytes and screenshot paths are omitted.
- A non-secure element exports one label, chosen and clipped exactly as
  `render._render_record` chooses it (`title or value`, cut to `text_limit`),
  so a field whose title and value differ never exports its value where the
  text path hides it.
- Every exported string passes the canonical redactor and the
  exfiltration-URL pass.

Tests seed secure records with a non-empty title, value and frame and assert
none of those bytes appear in either output. They seed a non-secure record
with distinct title and value and assert the value is absent from both, and
credential-shaped text on a non-secure element and assert both outputs redact
it. Each mutation is run: removing the secure branch or the label rule from an
adapter must fail its test.

The app reaches the structured entry through an app-token route in the same
namespace. The route refuses any call `dispatch_tool` would refuse, including
while the Computer Use keystone is off. That is not enough on its own:
`_dispatch` checks enablement, target policy and the SEL audit, but not who
the caller is. The existing HTTP entry (`dashboard/handlers/computer_use.py`)
refuses anything without `internal_auth`, because an app-scoped token could
otherwise reach it and pick its own `session_key`, and with it the governance
profile. The app route replaces that gate with three checks: a verified app
claim, a live task lease on the `desktop` surface issued to that same app, and
a session key the gateway derives from the lease, never one the body names.
A `permissions.api` prefix alone widens on an app update without a consent
moment, so the route also needs the owner's `desktop_text` grant. Whether it
needs a second opt-in inside `computer_use.json` is open question 1.

Desktop undo is weaker than browser undo, and the contract says so. The loop
may restore a field it set, or press Escape to close a menu it opened. Anything
else reports `restored=False`, and the loop stops with `needs_human` instead of
claiming the step was reversed. `Cmd+Z` is never used, because it can undo the
user's own work.

### 3.6 Browser: a read-only phase 1

In phase 1 the app drives its own headless Chromium. This is a new actor, so
phase 1 is read-only by construction. It does not rely on the loop's own
judgement:

- The profile is ephemeral and empty: no user cookies, local files, uploads,
  downloads, clipboard or extension APIs, and no popups.
- Only HTTP(S) destinations are allowed. Loopback, private and link-local
  addresses are refused.
- The loop may observe, scroll, follow links, open and close menus, and type
  into search fields.
- Submitting a form, activating a control that issues a non-GET request, a
  download, and any step the loop classifies as irreversible all stop the task
  with `needs_human`. No approval-and-resume path exists, so nothing waits for
  a person.

Jev's risk answer is extra defence and telemetry. It is not the authorization
boundary. A later phase may add a gateway-owned approval record and a resume
token, and gets its own section when it does.

In phase 2 the loop can drive the Electron panel the user is already watching.
The command bus (`browser/command_bus.py`) carries one op per call and returns a
snapshot rendered for a model. Phase 2 adds `snapshot_structured`, which returns
candidates with stable ids and a semantic signature each, and `act`, which
carries the expected signature and is refused on a mismatch or a stale id. The
bus contract stays one op, one result.

The panel is the user's own signed-in browser, so phase 2 needs a stronger
action bound than phase 1, not a weaker one. The phase 1 refusals (submit,
non-GET request, download, irreversible step) apply unchanged and are enforced
by the bus in the main process, not by the app. Anything beyond that waits on
the gateway-owned approval record deferred above, and phase 2 does not ship
before it.

## 4. Cost

JevOnly, measured on 2026-09-21 from a tree whose content was later merged as
commit `9746743`: about $0.014 per browser task (20
steps, 80 questions) and a 0.15 s median per Jev request. On a four-case QA
suite run twice, 8 of 8 passed in 70 seconds for about seven cents. The chat
model is called once to delegate and once to read the result, instead of once
per step, so the saving grows with task length. For a two-step task it is zero,
and the delegation skill says not to hand those over. The implementation PR
checks the task list, the repetitions, the failures and the cost accounting into
a result manifest pinned to that commit.

## 5. Security

Each property names where it is enforced.

- **The isolated browser cannot reach the user's logged-in state.** Enforced by
  the app: it launches an empty, ephemeral profile. Desktop actions remain
  behind the Computer Use keystone and the existing dispatch checks (core).
- **Phase 1 browsing cannot submit, buy, send or delete.** Enforced by the
  app's browser policy in its own Chromium, not by core. Jev's risk answer is
  not the control. In phase 2 the same refusals move into the command bus
  (core).
- **Structured desktop output is no wider than rendered output.** Enforced in
  core: one sanitized export type, pinned by mutation-tested secure-field,
  label and redaction tests.
- **Egress is scoped per surface and bound to an endpoint.** Enforced in core:
  both scopes live on the keystone, stay absent until granted, and are cleared
  on revoke. Questions go through the same scrub and governance ceiling as
  every other point.
- **The Jev key never leaves the gateway.** Enforced in core: the app only
  reaches Jev through the lease routes.
- **Third-party retention.** TypeSafe publishes no data-retention policy. The
  consent copy does not say or imply that page content is kept only
  briefly.

## 6. Alternatives considered

- **A decision point per step inside the agent turn.** Each `browser` call would
  ask Jev to pre-rank the snapshot for the model. That shrinks the tokens per
  step but keeps one model turn per step. In an offline test on three research
  questions, Jev's top 40 of 255 to 858 passages held 93% to 100% of the
  hand-labelled answers, which makes a snapshot pre-filter a good later point.
  It does not replace the executor.
- **The loop in core.** Rejected by the app tenet, and because the harness rules
  change with each site.
- **The app holds its own Jev key.** Rejected. It would bypass consent, the
  endpoint binding and the governance ceiling, which are the reason the seam
  exists.
- **Screenshots and OCR for the desktop.** Rejected. Pixels would leave the
  governed layer, and Jev does not read images.

## 7. Open questions

1. Is the Computer Use keystone enough for the app-token route into
   `dispatch_structured`, or does `computer_use.json` need a second opt-in for
   app callers?
2. What are the app-wide ceilings (live leases, questions per minute), and do
   they belong on the keystone or in config?
3. Should the delegation skill ship with the app, or as a built-in skill that
   names the app as optional?

## 8. Rollout

1. **This document**, as a proposal. Before any implementation PR opens, a
   maintainer accepts it and a separate base commit changes `status` to
   `accepted`. An implementation PR points at that record and never accepts the
   RFC in its own diff.
2. **Wire types.** `Noul` and `Score` in `decisions/types.py`, Jev and LLM wire
   mapping, a per-type branch in `gate._answers_are_valid`, malformed and
   partial-answer tests (including one proving a malformed answer returns
   `None` rather than raising), and `decisions.md` updated.
3. **Consent and task-executor routes.** The two scopes, the two point names,
   the lease routes and app-wide ceilings, and the task-summary row. Updates
   `decisions.md`, the App Kit API and manifest docs, every locale catalog, and
   the i18n tests. Tests cover app-token permission and enabled checks, and
   caller identity on every baseline-selectable harness.
4. **Structured Computer Use.** The executor split, `dispatch_structured` with
   its export type and route. Updates `computer-use.md`. Mutation tests cover
   secure-field removal and redaction on both adapters.
5. **The app.** JevOnly packaged for the registry with the two tools, the
   delegation skill, the phase 1 browser policy and its trace store.
6. **Phase 2 browser.** Blocked on the gateway-owned approval record (§3.6).
   Then the structured ops on the command bus, with the phase 1 refusals
   enforced in the bus. Updates the browser-owning spec. Tests cover signature
   mismatch, stale ids, refusal of private and local destinations, and refusal
   of a submit and a non-GET action in the panel.
