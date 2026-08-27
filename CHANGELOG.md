# Changelog

## 0.25.0 — 2026-08-27

Wave 0 of the programme opened after 0.24.0: hygiene, and one defect hiding
inside it.

- **The `health-migrated` marker is gone, not guarded.** The legacy import was
  gated to run once, ever, by a marker file beside the ledger. That is a second
  copy of a truth the ledger already holds, and the two could disagree: delete
  `health.db` and the marker survives, so the ledger never rebuilds and any
  legacy incident in the log is invisible for good. On this machine the marker
  read `0` against an absent ledger.

  Replaying is safe, and `open_incident` already guaranteed it — `incident_id`
  is the PRIMARY KEY, the insert is `ON CONFLICT DO NOTHING`, acknowledgement
  UPDATEs the row rather than deleting it, and legacy ids are derived from the
  record rather than minted per run. Its docstring says so outright: "the log it
  came from is replayed on every start." The marker was contradicting the
  invariant it appeared to protect. Removing it deletes state that could go
  stale instead of adding a guard to keep it fresh — the shape round 8 punished
  twice.

  The consequence was measured before it was called harmless: the live log holds
  **0** legacy incidents, so nothing was lost here. That measurement got a
  positive control, because a detector that finds nothing looks identical to a
  log that contains nothing — a synthetic pre-0.19 record was detected as
  `legacy:` on the same code path.

  Codex reported this in an earlier consult and it was never fixed; it was
  re-derived independently before the transcript was found.

- **Five tests, two of which failed for the stated reason first.** A deleted
  ledger now rebuilds from the log, and a replay does not reopen an acknowledged
  incident — the permanent-chatter failure 0.15.0 introduced and 0.16.0 had to
  cut back. That second test passed vacuously before the fix, because the marker
  short-circuited the second call; it only acquired teeth once the marker went.

- **README drift.** It advertised 493 default tests against an actual 690, in a
  repo whose own version guard warns that "a copy is a thing that silently
  drifts." The `dev/` harnesses 0.24.0 shipped are now documented there too.

## 0.24.0 — 2026-08-27

Two developer harnesses. Neither changes what the plugin does; both change what
can safely be done TO it. Written after a probe of this project's own trampoline
put a fake capture fault into the production log, where it then greeted every new
session for 24 hours.

- **`dev/probe_root.sh` runs a command against a deployed root with every
  side-effect channel closed, and proves they were closed.** The 2026-08-26
  probe drove the loop-refusal branch on purpose — the right instinct, proving a
  guard fires — with `ZOTERO_API_BASE` pointed at a dead port and the state
  directory still pointed at the live one. So it minted a real
  `forward-loop-refused` record in the production capture.log. A probe careful
  enough to build a negative control is exactly the probe that reaches
  production, because it deliberately drives the paths that only fire in anger.

  Nothing needed building. `ZOTERO_CAPTURE_STATE_DIR` was already honoured by
  every hook, and `sqlite_cache.py` already prints advice to use it; the knob
  existed and went unused, which is what a harness is for.

- **The probe's seam check runs first, on both channels.** Setting a variable the
  code under probe does not read is what makes a harness report a comfortable
  zero forever — `dev/measure_extraction.py` spent four releases swapping a regex
  nothing called. So the root's OWN expressions are evaluated: the shell's
  `ZP_LOG=` line, because the leak was a shell write rather than a Python one,
  and the Python `_state_dir`. If either resolves outside the sandbox the command
  is refused rather than run. All 25 cache roots honour the redirect back to
  0.1.0, but that is a grep, and a grep is not an execution.

- **`--control` proves the production fingerprint guard can fail.** A detector
  nobody has watched detect is not evidence: an unarmed guard reports the same
  clean run as a working one. Every guard in both scripts was mutated and watched
  failing for its stated reason before being trusted.

- **`dev/backport_trampoline.sh` is the 2026-08-26 backport, tracked.** A release
  cannot fix a root that predates it: 0.13.0 gave the hooks a trampoline and
  0.22.0 gave the command path one, and neither reached backwards, so fourteen
  live processes were holding 0.20.2's pre-guard `/triage`. The current launcher
  was copied by hand into 23 roots that night and the knowledge lived only in a
  memory file. Now it is reproducible, reviewable, and re-runnable after the next
  release lays down another root — dry-run by default, per-root backups, atomic
  rename because live processes re-read these scripts on every fire.

- **`--verify` asks behaviour, not grep.** A root is handed a script path that
  exists in neither root; whichever root the resulting error names is the root
  that ran. It writes nothing, and the pinned root is required to DECLINE to
  forward — without something capable of not forwarding, "everything forwards" is
  not a result. Roots that already carry an older trampoline are left alone
  deliberately: the only difference is an argv-remapping loop, and hooks are
  handed JSON on stdin with no argv.

## 0.23.0 — 2026-08-26

The round-7 findings 0.21.0 left open, cleared from the notes already in hand
rather than by buying another review. All six were re-verified against live
source first; none had drifted. Plus the defect 0.21.0 introduced and named in
its own changelog, and the one gap 0.22.0's trampoline could not structurally
close.

- **The pin is read before every write, not once per message.** The pin
  AUTHORISES a write — it is the entire basis for calling one healthy or stale —
  and a capture spans seconds of network I/O per URL. An upgrade landing inside
  that window left every write after it running from a root the registry no
  longer pinned, each recorded as healthy, because the authorisation had been
  cached before it went stale.

- **`issued = True` means a request actually went out.** Set before
  `_record_intent`, a refused journal — read-only state dir, full disk — left
  the claim held for a write that never happened, so every other session skipped
  that URL for the whole stale-claim window while nothing existed to complete it.

- **`ledger_path` is required.** 0.20.2 made the caller own it precisely because
  deriving it puts incidents where the checker never looks, then left a default
  that derives it for anyone who forgets. Two tests were quietly relying on that
  fallback — which is how the suite once wrote real incidents into the live
  ledger.

- **The ledger waits 1000 ms, not SQLite's default 5000.** A Stop hook has
  10000 ms for everything; two journalled URLs contending could spend the lot
  before any work happened, and the hook's `timeout` kill leaves no record at
  all — the outcome the ledger exists to prevent.

- **Legacy incidents get real ids.** `legacy:<second>|<root>` is the aliasing
  0.19.0 deleted, walking back in through the migration path: two writes from
  one root in one second collapsed onto one key, so acknowledging either
  silenced both. It is a digest of the record now — stable across reads, because
  the id is a handle a person types back.

- **Migration imports only records that could not journal themselves.** A record
  carrying an `incident_id` wrote its own ledger row; re-importing it under a
  capture-scoped key duplicates it. While the ledger key WAS the capture id that
  re-import collided and vanished, so the dedup was accidental, and 0.21.0's
  per-mutation ids removed the accident.

- **One complete unreadable line is reported.** `MIN_BROKEN_TAIL = 3` existed
  because `strip()` discarded the newline, which is the only evidence separating
  a half-written final line from a finished line that is not a record. Keeping
  it separates them: a torn final append is exempt, and the threshold drops
  to one.

- **Triage asks the staleness guard.** `stale_reason` appeared nowhere in
  cli.py. 0.22.0's trampoline covers this by forwarding, but only for roots that
  contain it and only when a target resolves — and the unresolvable case is
  exactly what staleness.py was kept for.

## 0.22.0 — 2026-08-26

Round 8 of external review, and a change of target: the maintenance passes.
Seven rounds had all audited capture and health. `repair`, `prune`, `retire`,
`backfill` and `run_triage` hold every destructive call in the project and had
never been read adversarially once. Nine defects, six of them High. Live
exposure was measured first and was nil — 4801 rows, none selected by any of
the broken predicates — so this is mechanism, not damage.

- **`--limit 0` applied the entire plan.** Both destructive entry points wrote
  `if args.limit:` against `default=None`, so 0 — the value someone reaches for
  when they want to be careful — was falsy and skipped the slice. Negatives are
  now refused too: `steps[:-1]` is every step but the last.

- **Retire deleted claims that were still in flight.** `zotero_key == ""` is
  also the normal state between `reserve_url()` and the POST returning, and
  `plan_retire` asserted the opposite. It dropped the row, capture's POST landed,
  `set_zotero_key` matched nothing, and the item sat in Zotero with nothing
  indexing it — invisible to dedup forever, so every later citation makes
  another copy. The planner could not even see the claim: `_read_rows` selected
  only url and key. A guard the production reader cannot feed is not a guard.

- **Capture now reads the answer it was already given.** `set_zotero_key` is a
  compare-and-swap returning True only if it took, and its result went unread —
  the one moment the system could notice an item had been stranded. It is
  recorded as a `claim_lost` error, which the health check surfaces.

- **Every finalize is compare-and-swap on the pair that was planned.** Retire
  and repair wrote `WHERE url_canonical = ?`, which touches whatever row holds
  that URL now. Another session can replace the row between plan and apply, so
  retire trashed K1 and deleted K2's row.

- **Destructive tools verify the index belongs to this library.** `bind_identity`
  was called in exactly one place: capture. Item keys are library-wide, so a
  client aimed at collection B still finds and trashes A's item, and nothing
  errors. These tools now VERIFY rather than bind — adopting an unvouched-for
  index during a destructive command means the first thing it does is have rows
  destroyed out of it. Gated on the apply, not the plan.

- **A destructive call re-checks the evidence it was selected on.** Optimistic
  versioning looked like it covered the snapshot-to-delete gap and does not: the
  version it sends is the one the final GET just fetched, so an edit landing
  before that is invisible. Prune could report trashing a font asset while
  actually trashing the paper the item had become. `trash_item` and `update_url`
  take `expect_url` and refuse when the item is no longer the one chosen.
  `trash_item` was also annotated `-> None` while two paths returned False.

- **The title resolver is asked about the item being written to.** It took no
  argument, so callers closed it over a snapshot URL while `add_tags` decides
  from the item's current state — backfill could write URL A's title onto B and
  clear the unresolved marker. It now receives the URL `add_tags` just read.

- **A repair carries the provenance across.** A rewrite left queued sightings
  under an address no row would revisit again; a merge deleted the row they were
  attached to; and a merge kept only the survivor's dates, discarding exactly
  the half of the history the duplicate carried.

- **The slash commands got the trampoline.** 0.13.0 covered the hooks and never
  the command path, so a superseded session ran superseded code that mutates the
  library. Unlike capture, a typed command has no availability argument for
  proceeding: it refuses loudly and exits 3. The forwarding also remaps
  root-qualified arguments, or delegation would delegate nothing.

Reserved names split by the tier's own definition: `localhost`, `.local`,
`.onion`, `.internal` and `.arpa` all resolve for whoever is on the right
network, so they are POLICY, not proof. `_is_reserved_name` is untouched —
capture is right to exclude them; only the destructive tier was wrong.

## 0.21.0 — 2026-08-26

Round 7 of external review, against 0.20.0-0.20.2 — the write-ahead ledger
itself. Two of the four High findings were defects inside the fix that round 6
told me to write. Both are fixed here; both were reproduced by execution before
anything was changed.

- **Each mutation now has its own ledger row.** The incident id was minted once
  per capture and reused for every URL in the message, and `incident_id` is the
  ledger's PRIMARY KEY with `ON CONFLICT DO NOTHING`. So a message citing three
  sources from a superseded root recorded ONE incident: it named the first URL,
  silently discarded the other two, and offered an `--ack` that closed evidence
  nobody was ever shown. Measured: three stale URLs, one row. A journal that
  records one entry per BATCH cannot describe a batch that partly succeeded,
  which is the only interesting case. The row id is now derived from the capture
  id and the URL — derived and not random, because DO NOTHING is load-bearing:
  a replayed write must not resurrect an acknowledged incident. The capture id
  stays on the log line, which is the thing that really is per capture.

- **A ledger that cannot be read is a fault, not a clean bill of health.** Every
  reader caught `sqlite3.DatabaseError` and returned an empty answer, so a
  truncated or half-written ledger reported `no open integrity incidents` and
  exited 0 — the evidence store for integrity incidents treated corruption of
  itself as proof of integrity. Measured on a real overwritten file:
  `count_open` 0, `--list-incidents` "none", `--ack-all` "resolved 0", exit 0.
  The readers now propagate. Nothing else was added: the top-level handler
  already turns an exception into a traceback and exit 3, and the session hook
  already turns non-zero into one visible sentence. A MISSING ledger stays
  silent, because a healthy install never opens an incident and so never creates
  the file — absent is zero, unreadable is unknown, and unknown is not zero.

Known and deliberately not fixed here: `_migrate_legacy` still synthesises
`legacy:<second>|<root>` ids, which alias two records from one root in the same
second (round 7, finding 4). While the ledger key was the capture id, a
migration re-import of a 0.19+ record collided with the row that record had
already written and was dropped as a duplicate; with per-mutation keys it no
longer collides, so a log migrated AFTER its own captures can now count one
capture twice. That over-reports a real event rather than hiding one, and every
row is listed by id, so it is visible and clearable — the honest fix belongs
with finding 4.

## 0.20.2 — 2026-08-25

The 0.20.1 fix caused a worse problem than the one it fixed, and the live ledger
is what showed it.

0.20.1 corrected a real divergence — the capture path derived the ledger from
`log_path.parent` while the health checker derived it from the state directory —
by making the capture path read `_state_dir(os.environ)`. But `run_capture` is
given its paths explicitly, and reaching into the environment for a sibling of
them meant the TEST SUITE wrote two genuine integrity incidents into the
developer's live ledger, against a fixture URL, from the repo checkout.

The caller now owns the path: `main()` resolves the state directory, which it
already does for the log, and passes both down together — so writer and reader
still agree, and nothing given explicit paths consults the environment behind
them.

Verified by deleting the live ledger, running all 617 tests, and confirming it
stays absent.

## 0.20.1 — 2026-08-25

Two holes in 0.20.0's own journal, both found by asking where it could still
fail rather than by a test.

- **A write is now refused when its incident cannot be recorded.** The intent
  was journalled before each mutation, but a failure to journal was caught and
  logged while the write went ahead — so a read-only state directory, a full
  disk or a permission error reproduced exactly the condition the journal
  exists to prevent, silently. A skipped citation is recoverable; an unrecorded
  mutation is not. A healthy capture opens no incident, so a broken ledger
  cannot stop ordinary work.

- **The writer and the reader now agree where the ledger lives.** The capture
  path derived it from `log_path.parent` and the health checker from the state
  directory. With `--log-path` pointing elsewhere, incidents were written where
  the checker never looked.

Concurrency was measured rather than assumed: 240 concurrent writers recorded
240 of 240 incidents with no failures.

## 0.20.0 — 2026-08-25

The sixth audit found something none of the previous five did: a defect in the
capture path rather than in the reporting. **The plugin could mutate the library
and then have no record that it did.**

- **The incident is written before the write.** The id was minted inside
  `_emit_log`, which runs after every Zotero call, so a hook timeout — the hooks
  impose ten and fifteen seconds — could leave a row created from a superseded
  root with nothing anywhere saying so. Worse, "did it write" was inferred from
  `urls_new + urls_recurring`, which are COMPLETION counters incremented after
  the POST returns and after local bookkeeping; a commit followed by an
  ambiguous failure reported zero writes. The incident is now journalled to a
  SQLite ledger before each external mutation.

  The asymmetry that settles it: an intent for a mutation that never happens is
  a false positive a person closes in one command; a mutation with no intent is
  corruption nobody can find.

- **Open incidents are the bounded thing, not the acknowledgement set.**
  Replaying an immutable, unbounded log and filtering acknowledged ids requires
  remembering an unbounded set of ids — there is no bounded lossless version.
  Acknowledgement now resolves a ledger row. A healthy install opens no
  incidents at all, so it stores nothing.

- **`--ack` of an unknown id fails.** It used to append the string to a file,
  print "acknowledged 1 incident(s)", and leave the real incident open behind a
  typo. `--list-incidents` pages with an explicit remainder rather than a silent
  cap, and the mode flags are mutually exclusive.

- **Proven pre-0.19 incidents are migrated rather than dropped.** A 0.15–0.18
  record with `root != pinned_root` and evidence of a write already proved an
  incident; only its acknowledgement identity was missing. Rejecting it for
  having no id silently suppressed every open incident at the moment of upgrade.

- **A broken log TAIL is reported.** Warning only when the whole log was
  unparseable let one old valid record bless an indefinitely broken telemetry
  stream. A single unparseable last line is still ignored — a torn final record
  is ordinary when the writer appends while the reader reads.

- **A bad clock no longer hides integrity evidence.** A future-dated record
  incremented the clock counter and skipped the rest, discarding classification
  that does not depend on recency at all.

## 0.19.0 — 2026-08-25

Fifth audit. Both High findings were in `--ack` — the mechanism added one
release earlier to replace the acknowledgement cursor removed the release
before that. This is the first round where the answer was to build the thing
properly rather than to remove it, because suppression here is genuinely
necessary: without it a repaired incident nags forever, and with the previous
design it silenced incidents nobody was ever shown.

- **Incidents are identified by their writer.** The key was
  `timestamp-to-the-second | root`, which aliased distinct incidents: a stale
  write and an unverifiable write from one root in one second produced ONE key,
  so acknowledging either silenced both — permanently, and without the second
  ever being displayed. Every capture now carries a unique `incident_id`
  written at capture time. A record without one is not classified, the same
  rule already applied to records with no pin evidence.

- **`--ack` was an undocumented `--ack-all`.** It cleared every incident in the
  log, while the report shows only aggregate counts and the newest incident.
  There is now `--list-incidents` (ids, kind, root, acknowledged state),
  `--ack ID...` for named incidents, and an explicit `--ack-all`. Arguments go
  through `argparse`, so a typo like `--akc` exits 2 instead of silently
  performing a normal run.

- **A capture that wrote nothing is not an integrity incident.** `urls_seen: 0`
  with an unverifiable pin produced a permanent warning demanding
  acknowledgement — a nag about an event that never happened. Integrity now
  requires evidence of an actual write.

- **A readable log with no parseable records is not healthy.** It returned exit
  0 and printed nothing, so format-incompatible was indistinguishable from
  fine — the same blindness as an unreadable log, one layer up.

- **Reading is streaming end to end.** `evaluate()` was made flat-memory last
  release, but the reader still called `read().splitlines()` first — about
  120 MB for half a million lines, defeating the evaluator behind it. A 56 MB
  log now streams at 0.02 MB peak.

- **Future-dated records are a clock problem, not a recent fault.** A record
  dated 2099 sat inside every recency window and would have reported for
  seventy-three years.

- **`registry.py` refuses malformed entries**, matching what the shell
  trampolines already did. Two resolvers claiming one policy and quietly
  disagreeing is how the original first-prefix-match bug survived so long.

## 0.18.0 — 2026-08-25

The fourth audit found the same bug class it had found in the first, in a
different place. That is the finding, and this release removes the class rather
than the instance.

Every defect this check has produced came from **cross-record inference** — one
record's meaning depending on another:

- a later successful capture "cleared" an earlier refusal, so one session's
  activity declared another session's stranded hook recovered (verified: an
  11:41 success erased an 11:40 refusal, and same-second appends lost their
  order entirely)
- an install timestamp scoped away stale writes, including one logged in the
  same second as the install, because log stamps carry seconds and the registry
  carries milliseconds
- before that, an acknowledgement cursor suppressed by timestamp
- before that, only the newest capture was examined at all

Each fix corrected one instance and the mechanism produced the next. So now a
record is judged only by its own contents, under two rules:

- **Operational faults decay.** Refusals and capture errors are reported inside
  a recency window. Timing a presence is sound; timing an absence is what failed
  twice before, and nothing here does it.
- **Integrity incidents do not.** A stale or unverifiable write stands until a
  person acknowledges it with `--ack`. Nothing automatic clears it, because
  nothing automatic knows whether the rows were checked — and a dead session's
  bad write is still a bad row.

- **A record with no pin evidence is not classified.** Found by running the new
  check against the real log rather than by a test: it called eighteen correct
  captures stale, because they predate the `pinned_root` field and were being
  compared to today's pin. That was the same unsound inference, smuggled in
  through a legacy fallback.

- **The monitor no longer reads its own blindness as health.** Every `OSError`
  on the log meant exit 0 and silence — permission denied, a directory in place
  of the file, a failing disk. Only a _missing_ log is healthy now. The bootstrap
  event emitter also reports its own failure instead of swallowing it.

- **The reader streams.** Materialising the whole log cost about 0.9 s and
  ~294 MB at half a million records, which would eventually trip the hook's own
  timeout and leave the monitor reporting nothing but its own failure. Peak
  memory over a 500,000-record generator is now flat.

- **Registry entries must be well-formed.** A malformed object (`{}` or
  `{"installPath": 7}`) was silently skipped rather than refused, contradicting
  the stated policy. And when two entries name the same root with different
  `lastUpdated`, the newest is taken rather than whichever was serialised first.

## 0.17.0 — 2026-08-25

A third audit. The pattern across all three is now the point: every fix to the
health check bought a new failure mode, because each one added machinery to
suppress the previous one's noise. This release removes the machinery instead.

- **The acknowledgement cursor is gone.** It was added in 0.16.0 so a stale-write
  incident would be reported once. It could not be made race-safe: log stamps
  carry one-second precision, so a record appended in the same second as the
  acknowledgement was dropped **permanently** — and concurrent sessions make
  same-second writes ordinary. It also advanced past records it had never
  classified, so a legacy record that became provable later was suppressed
  forever.

  Scope replaces state. An incident is reported while it still describes the
  generation now installed, and an upgrade retires it. Nothing is stored, so
  nothing can be lost. This does mean a stale write from before the current
  install is not reported — accepted, because it is self-correcting for the case
  that can still be acted on: a session still alive writes again, and that write
  is reported.

- **A capture that could not verify its pin is its own warning.** 0.16.0 wrote
  `pin_observation: "unknown"` and read it nowhere, so a record that explicitly
  disclaimed knowledge was judged by the legacy clock and produced a positive
  stale warning. It is now neither stale nor silent.

- **Configuration and bootstrap failures reach the log as events.** A missing
  credential wrote one plaintext line, which the health parser drops for not
  being JSON — so a fresh install with no API key could fail on every cited URL
  forever without a word. `configuration-error` and `capture-bootstrap-error`
  are now structured, written through a state directory that resolves without
  credentials, which is the point.

- **The health hook says when it cannot start.** Missing `jq`, a corrupt
  `lib.sh` and an absent interpreter all exited 0 in silence. The `jq` case was
  the worst: no health hook could resolve a target, so nothing was left able to
  report the `forward-unresolved` events the capture hooks were writing.

- **`jq`'s exit status is checked.** Process substitution hides it. With a valid
  entry followed by a malformed one, `jq` printed the good path and then exited
  5; the loop counted one candidate and accepted it. Reversing the entries
  refused. Resolution was serialisation-order dependent again — the same class
  as the first-prefix-match bug that started this sequence. A malformed entry
  anywhere in the array now refuses outright.

- **The three inline trampolines are held byte-identical by a test.** Inlining
  is still right at runtime, but three copies of the arithmetic that decides
  what gets `exec`'d will drift. The per-hook difference now lives in one named
  seam. The parity test found drift on its first run.

## 0.16.0 — 2026-08-25

A second audit, of the fixes from the first. Seven findings; the health check
failed again, and this time the fix for the previous round's worst bug had
created its mirror image. So the detector was cut back rather than built out.

**The capture path — two real bypasses.**

- **A symlinked `$HOME` switched the trampoline off entirely.** `ZP_MINE` was
  canonicalised with `pwd -P` and then compared against a prefix built from a
  raw `$HOME`. With `/home/alice -> /srv/users/alice` the two never match, so
  every superseded root was classified as a development checkout and ran its own
  stale code — the v0.3.0 failure, reintroduced by the guard written to prevent
  it. Reproduced against a real symlinked home before fixing.
- **Two spellings of one root refused every capture.** The shell deduplicated
  registry entries as strings before canonicalising them, so `/p/1` and
  `/p/1/../1` looked like two candidates and the ambiguity rule declined. Each
  candidate is now canonicalised before they are compared, matching what the
  Python resolver already did.
- A malformed `{"plugins": [1]}` crashed the resolver. Valid JSON, and the
  traceback went to a stderr the hook discarded.

**The health check — cut back to what a log can prove.**

Scanning every record fixed "a later good capture hides a stale one" and
immediately created the opposite fault: one stale record in an unbounded log
warned at every session start, forever, long after the session that wrote it had
exited. A record proves a **write** happened; it never proved a session is still
live.

- The claim shrank to "a capture occurred from a version that was not installed
  at the time", and it is reported **once**, against an acknowledgement cursor.
- **Hook-fire counting is gone**, and with it the heartbeat file. It could not
  see the case it was added for — with no valid capture record the checker
  returned before ever reading it — and it would have chattered after about a
  hundred URL-free turns anyway. Two designs that lied in opposite directions
  were enough; the third is smaller than both.
- A record carrying `pinned_root` is now read **without** the current registry.
  It was already proof, and gating it on a readable registry threw that away.
- Refusal counts are capped in the message rather than printed in full.

**Honesty about the instrument itself.**

- **The checker exits non-zero when it fails**, and the hook turns that into one
  stable sentence plus a `health-errors.log`. It used to exit 0 after writing to
  a discarded stderr, so an internal crash was byte-identical to a clean bill of
  health — the exact failure class this feature exists to report, reproduced
  inside the feature.
- **The health hook now delegates like the capture hooks do.** The previous
  version argued it need not, because its inputs are global. That confused
  global inputs with version-independent logic: 0.14.0, 0.14.1 and 0.15.0 all
  disagree on identical input.
- **The pinned root is observed before the capture, not after.** Reading it
  afterwards let a registry change mid-capture record a pin the write never ran
  under, fabricating a stale write that never happened — and, reversed, hiding a
  real one. When it cannot be resolved the record says `pin_observation:
"unknown"` rather than silently omitting the field.

Hook cost fell from about 37 ms to about 26 ms per fire, measured interleaved,
by replacing four `basename`/`dirname` subshells with parameter expansion and
dropping the heartbeat write.

## 0.15.0 — 2026-08-25

An external audit of yesterday's three releases. Six findings, four of them
High, and the worst were all in the health check — the feature built two hours
earlier specifically to detect silent failure. Every one reproduced before being
accepted.

- **A later good capture erased the evidence of a stale one.** The check read
  only the newest capture. Two sessions running concurrently — a lingering
  superseded one that captures at 11:40, a current one that captures at 11:41 —
  and the stale write became invisible permanently, while that session was still
  alive and still writing with rules corrected releases ago. Every capture since
  the install is now examined, not just the last.

- **A capture now records the pinned root it observed when it wrote.** The
  install timestamp was never a generation clock: any upgrade or reinstall
  advanced it, retroactively forgiving every write that had already been stale
  when it happened. Staleness is now a property of the record, decided once by
  the process that was there. Lines written before this fall back to the old
  comparison.

- **Registry resolution asked for a prefix and took the first hit.** A registry
  legitimately holding this plugin from two marketplaces, or at two scopes,
  resolved to whichever was serialised first — and the trampoline `exec`s that
  path. Identity is now exact and derived from the caller's own location: a root
  at `.../cache/<marketplace>/<plugin>/<version>` resolves only
  `<plugin>@<marketplace>`, only to a canonical target inside that same subtree,
  and refuses outright when two entries disagree.

  Both sides are canonicalised first. A trailing slash or symlink spelling made
  a root unequal to itself, so it forwarded to itself and hit the recursion
  guard — losing not one capture but every capture for the life of the session.

- **Refusals looked exactly like successes.** The two most deliberate failures —
  a superseded version, and an index bound to a different library — logged an
  error and returned an empty result, after which the CLI wrote an ordinary
  `urls_seen: 0, errors: []` line. That is the shape of a healthy message with
  no citable URL, so the health check counted a refusal as a capture and reset
  its own clock. An identity mismatch could refuse every citation forever while
  writing a healthy-looking record each time. Refusal is now a field on the
  result, in the log line, and read by the check.

- **`missing-dependencies` was written without a timestamp**, and the check
  discards any record it cannot place in time — so on a fresh install with
  missing dependencies it would have stayed silent indefinitely. Relatedly,
  `date -Iseconds` is GNU-only; on stock macOS it produced no timestamp at all.
  Both now use `date '+%Y-%m-%dT%H:%M:%S%z'`.

- **Wall-clock silence is gone.** A 24-hour threshold warned after any ordinary
  weekend and repeated across SessionStart's resume/clear/compact/fork subtypes.
  Elapsed time measures the user's habits; hook fires measure the plugin. The
  hooks now append a heartbeat line per fire, and the check reports many fires
  with no successful capture between them.

Verified by real execution, not only by tests: silent against the live log,
and reporting correctly against replays of each failure above.

## 0.14.1 — 2026-08-25

The health check cried wolf on its own release, and that was caught by running
it against the live log rather than by a test.

0.14.0 warned whenever the most recent capture came from a plugin root that is
not the installed one. Thirty seconds after shipping it, it fired: the newest
capture had come from 0.13.0 because it happened _before_ 0.14.0 was pinned.
Nothing was wrong. Left alone, the check would have raised a false alarm on
every release it ever saw — which is precisely the way a check earns being
ignored, and the failure mode 0.14.0's own notes said would kill it.

The signal was asking the wrong question. "Did the last capture come from an
unpinned root" has a legitimate yes right after any upgrade; the real question
is whether anything has captured from an unpinned root _since_ the upgrade. The
check now takes the registry's own `lastUpdated` and suppresses the warning for
captures that predate it. The other three signals — refusals, silence, errors —
were never affected and still fire.

Verified both directions afterwards: silent against the real log, and still
reporting the refusals and the 29-hour gap against a replay of the outage.

## 0.14.0 — 2026-08-25

The plugin can now tell you it is broken.

Every serious failure this plugin has had was silent. A v0.3.0 root wrote junk
into the library for weeks. Capture stopped completely for 29 hours. Both were
found by an audit, not by the plugin noticing anything was wrong. `staleness.py`
has stated the principle since 0.11.7 — "loud absence beats quiet corruption" —
but nothing was ever watching for the absence, so the corruption stayed quiet.

- **A `SessionStart` health check.** It reads the capture log and speaks when,
  and only when, one of four things is true: the last capture ran from a plugin
  root that is not the installed one; refusal events have been recorded since
  the last successful capture; nothing has been captured for longer than the
  silence threshold (24 h, `ZOTERO_CAPTURE_MAX_SILENCE_HOURS`); or the last
  capture reported errors.

  It reads what the log has carried since 0.12.0 rather than adding new
  bookkeeping, so it can answer for the past as well as the present. Verified
  against a replay of the actual 2026-08-25 outage: it reports the stale root,
  the 91 refusals, and the 29-hour gap.

- **Silence is the requirement, not a nicety.** On a healthy session it prints
  nothing. A check that speaks when things are fine gets tuned out, and a
  tuned-out check is worse than none — which is precisely how the measurement
  canary printed its warning for four consecutive releases without anyone acting
  on it. The silence test is the most important one in the suite and the one
  most likely to rot, so it is named as such.

- **It cannot break a session start.** No credentials are loaded, since
  requiring a working config would silence the check in some of the cases it
  exists to report. Every path exits 0. `ZOTERO_CAPTURE_HEALTH_DISABLE=1` turns
  it off without touching capture.

- **No trampoline on this hook, deliberately.** Unlike the capture hooks, it
  reads the log and the registry — both global rather than per-root — so a
  superseded copy reaches the same conclusion as a current one, and it never
  writes to the library.

## 0.13.0 — 2026-08-25

A superseded plugin root now delegates to the installed one instead of refusing.

0.12.0 shipped a guard that made an out-of-date root decline to write, which was
right as far as it went: stale code cannot know its rules were corrected, and
loud absence beats quiet corruption. But it left every live session dark until
that session restarted, and a session cannot be made to re-resolve its plugin
root. On 2026-08-25 that meant 18 sessions capturing nothing, on top of the 29
hours the previous release's containment had already cost.

The thing both earlier passes missed is that "restart required" pins the plugin
ROOT, not the hook SCRIPT. The script is re-read from disk on every fire. The
log shows it plainly: the last capture from the pre-guard roots was at 00:31:37
on 2026-08-24 and the first refusal at 00:32:57 — eighty seconds later, from
sessions that had been running since 08-21, with no restart in between. Whatever
is in that file at the moment it fires is what runs.

- **A trampoline at the top of `capture-stop.sh` and `capture-prompt.sh`.** If
  this root is not the one `installed_plugins.json` pins, the hook `exec`s the
  same hook in the root that _is_ pinned, and the current rules apply. Nothing
  is deleted and nothing restarts.

  The authority is the pinned `installPath`, not a version comparison: it is
  what the manager actually resolves, it needs no parsing, and it follows a
  rollback in the right direction.

  It is inline in both hooks rather than factored into `lib.sh` on purpose — a
  root that predates this code has a `lib.sh` that predates it too, and the
  point is to be correctable by replacing the file that actually runs.

- **A development checkout never forwards.** Forwarding is limited to roots
  under the plugin cache, so running the hooks from a clone exercises the code
  in front of you rather than whatever is deployed. This plugin has twice been
  confused about which copy was executing; the fix should not add a third way.

- **Three ways it declines rather than guesses**, each logged with its own
  event and each exiting 0: `forward-loop-refused` if a forward lands back on a
  superseded root, `forward-unresolved` if no target can be read, and the
  existing `staleness.py` guard, kept as defence in depth for exactly that last
  case.

- **`staleness.py` no longer claims old roots must be deleted.** It concluded
  that "there is no way to reason with them"; deletion was tried on 08-24 and
  caused the 29-hour outage. You do not have to reason with old code to replace
  the entry point that reaches it.

## 0.12.0 — 2026-08-25

An external audit of the whole directory, after the previous one was scoped to
URL handling. Eight findings, all reproduced before being accepted. The two that
matter most are not bugs in the code so much as bugs in what the code could
know about itself.

- **The plugin had not captured anything for 29 hours, and said nothing.**
  Claude's plugin registry still pinned `zotero-provenance` to the v0.3.0 cache
  root. 0.11.7 had neutered every pre-guard root — correctly — but that root was
  the one every new session resolved, so the containment took the whole plugin
  down with it. 92 refusals on 2026-08-25, zero captures since 00:31 the day
  before. Fixed by `claude plugin update`; the registry now points at the
  current root.

  The lesson generalises past this bug: "deployed" and "executing" are different
  questions, and neither the repo nor the marketplace clone can answer the
  second. Every capture log line now carries the `version` and `root` that wrote
  it, plus the library and collection it wrote to. Finding the v0.3.0 session
  took replaying a URL through nine cached versions; with these fields it is one
  grep.

- **The measurement harness had been measuring nothing since 0.11.3.** It
  swapped `up.URL_RE`, and the linkify rewrite left that regex vestigial. So
  `--control` — whose only job is to prove the instrument can fail — reported
  zero damage, and every "changes nothing on real traffic" claim after that was
  vacuous. The canary printed its warning each time and nobody re-ran the
  control after changing the architecture. The control now replaces `bare_urls`,
  and a seam check runs against a fixture BEFORE the corpus, so a disconnected
  injection point fails loudly. Restored: control damages 205 of 1315 real
  messages, the shipped extractor 1.

- **Four parser holes**, three of which the working harness or the audit found:
  a template inside a link destination was captured (CommonMark percent-encodes
  it, so `{ID}` arrived as `%7BID%7D` and the brace rule saw nothing — this is
  how `files.rcsb.org/download/%7BID%7D.pdb` reached the library); one
  impossible port raised out of `canonicalize` and discarded every citation in
  the message; raw HTML anchors were skipped as "raw" alongside code spans,
  conflating showing a URL with linking to one; and an unbalanced `(` stored a
  silent truncation, since linkify balances parens and `(` is a legal
  sub-delimiter.

- **Queued provenance was lost on any Zotero error, and again on success.**
  `take_pending_tags` is a destructive read and it was called as an ARGUMENT to
  `add_tags`, so the DELETE committed before the request went out. Split into
  peek + clear. Separately, a completing session never drained what another had
  queued against its in-flight claim — so a URL cited once, simultaneously, by
  two sessions silently lost one session's record forever.

- **Reservations had no owner.** `set_zotero_key` and `release_url` matched on
  URL alone, which was safe only until claims could be reaped. Both are
  compare-and-swap on the expected `pending_key` now.

- **The index did not know which library it indexed.** Changing collection split
  sources in half (a recurring URL is only tagged, and tagging does not move an
  item); changing library made every row a permanent 404. It now binds
  `(api_origin, library_type, library_id, collection_key)` and refuses a
  mismatch. An unbound index adopts rather than refuses, because the deployed
  one holds 4,732 rows that predate this and cannot prove their origin.

- **A person editing the library broke the index permanently.** A trashed item
  made every future citation repeat the same 404 forever; a title fixed by hand
  was overwritten because the stale `title:unresolved` tag was trusted over the
  title itself; and `update_url` reported success on a 404, so repair recorded
  rewrites for items that do not exist.

- **The retry queue was dead and unsafe.** `append_failure` was called only by
  its own tests, so nothing was ever enqueued, while the drain rewrote the file
  with no locking. Four green tests asserted a safety net that was not attached.
  Deleted rather than repaired.

- **A documented install could not run the code.** The README asked for httpx,
  beautifulsoup4 and jq; the code imports `idna`, `linkify_it` and `markdown_it`
  at startup, so a user following it exactly got an ImportError before any of
  this plugin's error handling. The hooks check imports first and name what is
  missing. `lib.sh` also claimed setup creates a virtualenv, which it has never
  done. Both hooks called GNU `timeout`, absent on stock macOS — a total silent
  outage on a supported platform that no test here could catch.

- **The staleness guard was a boolean with two holes.** It asked "is running
  older", so a root NEWER than the install passed — yet rolling the install back
  is exactly how a bad release is stopped, and under that rule the rollback did
  nothing. And an unparseable version returned the same value as "verified
  current", silently disabling the guard. Now three-state: exact agreement is
  CURRENT, anything else is MISMATCH, and UNKNOWN still proceeds — the one place
  this plugin inverts refuse-on-doubt — but logs that it did.

493 tests pass.

## 0.11.7 — 2026-08-24

- **A stale plugin root refuses to write to the library.** Every URL defect
  fixed in the six releases before this one was still reaching the collection,
  because the code fixing them was not the code running. The capture hook
  executes a cache entry keyed by version, and a session holds whichever entry
  it resolved at its own start — so a session still on **v0.3.0** wrote eight
  junk URLs on 2026-08-23, `https://example.org/bar` among them. Every release
  since v0.8 refuses that address; v0.3.0 predates the rule, so nothing stopped
  it, and nothing reported a problem either. The index gained rows and the log
  recorded a successful capture.

  Capture now compares the version it is executing against the installed clone's
  manifest and declines, with a log line naming both, when it is behind. Loud
  absence beats quiet corruption.

  Two deliberate choices. The comparison is numeric, so 1.10.0 is newer than
  1.2.3 rather than the reverse. And an unreadable version fails **open** — the
  opposite of this plugin's usual rule — because a false positive silently
  switches capture off for someone whose install layout could not be read, which
  is worse than the rare stale write it would have caught.

  Its limit, stated rather than discovered later: **a guard cannot fix a version
  that predates it.** v0.3.0 will never refuse itself. This closes the door from
  here forward; older roots have to be removed from disk, because there is no
  way to reason with them.

## 0.11.6 — 2026-08-24

An external audit of 0.11.5 found nine defects, five of them silent corruption
and three introduced by the releases immediately before it. Every one was
reproduced against HEAD before being accepted.

- **Where a bare URL ends is now linkify-it-py's judgement, not ours.** Three
  hand-rolled attempts at that boundary each shipped a corruption:

  `https://en.wikipedia.org/wiki/People's_Republic_of_China` was stored as
  `.../wiki/People` — a DIFFERENT real Wikipedia page, so it resolved a
  plausible title and read as a citation nobody made. The apostrophe is an RFC
  3986 sub-delimiter; excluding it on seven corpus observations did not survive
  one live counterexample. `{{ID}}.pdb` defeated the continuation guard,
  because that guard looked exactly one character past the illegal one and the
  next character was also illegal. And a curly quote, an em dash and U+00A0 were
  all absorbed into the address — the IRI range began AT the non-breaking space,
  so a match could cross a visible word boundary.

  linkify-it-py is markdown-it-py's own linkifier. Matches are sliced out of the
  original text rather than read from a token href, because going through
  markdown-it percent-encodes the result (`München` → `M%C3%BCnchen`), which
  changes the dedup key and would duplicate every non-ASCII row already stored.
  A bracketed IPv6 literal keeps its own pattern and is taken out of the text
  first: linkify does not recognise that form at all.

  The URI grammar keeps a job, a different one — it VALIDATES what linkify
  delimited instead of deciding the extent.

  Measured over the same 1,370 real messages: **4,009 URLs before, 4,009 after,
  one message different**, and that one is an elided URL containing a literal
  `...` and an unbalanced paren, junk under both. Disabling bare matching
  drops 852, so the comparison can see a difference.

  A known cost, recorded rather than hidden: an _unpadded_ table cell
  (`|repo|https://…|`) is no longer captured, because linkify needs a boundary
  in front of the scheme. Padded rows, which is what generators emit, work.
  Losing a citation is loud absence; the alternative was the corruptions above.

- **Repair can no longer emit what capture would reject.** It shared no
  predicate with the tokenizer, so `…/filter[name]|` was "repaired" to
  `…/filter` — shorter, resolvable, and something extraction would never have
  produced. Both paths now strip the same illegal tail and ask the same
  `is_storable_url`. An illegal character in the MIDDLE is refused outright
  rather than truncated into an address nobody cited.

- **ANSI sequences are removed, not cut at.** An escape wraps an address rather
  than ending one, so `…/a\x1b[31mcontinued` is one URL wearing a colour code;
  cutting at the ESC invented the shorter one. CSI, OSC (including OSC 8
  hyperlinks) and two-byte escapes are all stripped, in capture and in repair.

- **A merge is refused unless the survivor is a real item.** `plan_repair`
  chose merge because the corrected URL was present in the index — not because
  that row's claim had ever completed. With an empty `zotero_key` the tag carry
  was skipped and the duplicate was trashed anyway, destroying the only real item
  and leaving an orphan row. It now requires a nonempty key that `item_exists`
  confirms, decided at apply time, and skips otherwise. Never a downgrade to
  rewrite: the corrected URL already holds the primary key, so the UPDATE would
  fail after the Zotero item had already changed.

- **A title counts only once its closing tag has arrived.** Making a blown
  deadline `break` in 0.11.5 fixed one problem and created a worse one — the
  partial body still went to BeautifulSoup, which accepts an unclosed
  `<title>`, so `Real Tit` was stored as resolved metadata. That is worse
  than storing the URL: it clears `title:unresolved` and nothing revisits the
  item. Matching the whole element also makes the stop case-insensitive and stops
  a stray `</title>` inside a script ending the read early.

- **Retirement is now two tiers, and its reversibility claim is honest.** HARD is
  proof the text cannot be an address — a placeholder, a control byte, a reserved
  name, a host no resolver could look up — and applies by default. POLICY is a
  real address this collection declines to keep — an asset, a font CDN, an
  intranet or private name — which CAN resolve for whoever is on that network, so
  it is opt-in behind `--policy`. Declining to capture something going forward
  is a weaker claim than reaching back and trashing what is stored.

  The docs said the pass was recoverable from any Zotero client. Only its Zotero
  half is: the trash does not hold `first_seen`, `last_seen` or queued
  provenance. Every applied run now journals each removed row to
  `url_index.db.retired.jsonl` before destroying anything.

  The dotless rule is stated as what it is. Not "can never identify a document" —
  a local DNS zone or a corporate proxy makes `https://wiki/runbook` perfectly
  real for whoever is on that network — but "this collection tracks globally
  addressable sources", which is a policy.

## 0.11.5 — 2026-08-23

- **A title behind a large inline script is no longer missed.** The reader
  stopped at 32 KiB. experian.com serves 200 with a perfectly good `<title>` —
  at byte 167,895, behind a long inline script — so three rows in the live index
  stored their URL as the title, which is precisely the junk this plugin exists
  to remove. The cap was the cause and the 1s budget was not: 32 KiB arrived in
  0.49s and the whole 271 KB page in 0.63s.

  The cap is now 256 KiB, and the read stops the moment `</title>` arrives, so
  an ordinary page still reads about a kilobyte and pays nothing for the higher
  ceiling. A blown deadline now _stops_ the read instead of discarding it —
  without that, raising the cap would have made things worse for a slow page,
  which used to stop at 32 KiB with a title in hand and would instead have
  streamed past the clock and thrown it away.

  Verified against the live sites: experian.com/help/credit-freeze,
  experian.com/protection/creditlock and a Yahoo Finance article all resolve
  now; myaccount.google.com correctly does not, since it needs a login.

  The existing cap test was written against a literal 32 KiB and would have
  silently become a test of nothing. It is written against `MAX_BYTES` now.

### Triage of what remains

16 items carried `title:unresolved`. Every one was fetched, against a positive
control — ten straight failures is also what a broken fetcher looks like, and
four of the five controls resolved, including the same hosts as some failures.

**10 of the 16 now resolve** and were rewritten in place: the three experian.com
pages the read cap had been losing, two Yahoo Finance articles, Fortune,
tradingkey, fxleaders, and two Google MyActivity pages.

Two causes were ours. The read cap above, and one marginal case: fxleaders.com
completes in 0.99s against the 1s budget, so it missed on a loaded pass and
resolved on the next. Nothing was changed for that — a retry is the right answer
to a page that is simply slow, and widening the budget would cost every capture.

The remaining 6 cannot resolve, for reasons that are not the plugin's:

- HTTP 404 — a private repository's pull request, and a Wikipedia page that never
  existed (one of this repo's own test fixtures, still in the library);
- authentication — `myaccount.google.com` and `myadcenter.google.com`;
- HTTP 403 — `tradersunion.com` refuses non-browser clients;
- a broken certificate chain on `mirror.oit.ncsu.edu`, which fails identically
  under plain `httpx`, so it is the host's and not the guard's.

An earlier note here recorded Fortune as a dead 404. That was wrong: the probe
that produced it used a URL truncated by the terminal listing it came from, not
the URL actually stored. Fetched properly, it resolves.

## 0.11.4 — 2026-08-23

- **A host with no dot is never a public document.** 19 rows in the live index
  had one and not one was a source: intranet services (`prometheus:9090`,
  `homelab:3000`, Ollama on `host:11434`), a machine name, and this repo's
  own test fixtures (`https://h/R&D`, `https://a`). A single-label name
  resolves only inside a network that already knows it, so it cannot identify a
  document anyone else can read — the same thing the localhost, tailnet and
  reserved-name rules already say. It goes with them rather than becoming a new
  kind of check, which also means `prune` sweeps the backlog with it.

  The rule runs only after an IP literal has been ruled out. A bracketed IPv6
  host has no dot either, and catching it here would exclude every IPv6 URL —
  the same damage as the `]` truncation that once stored them all as
  `https://[::1`. There is a test for exactly that.

- **A merge no longer carries the duplicate's title state onto the survivor.**
  Repairing `…/ARFDSynInt.git|` merged it into the clean row and tagged that
  row `title:unresolved` — although its title was perfectly good — because the
  merge carried every tag across. Provenance (`project:`, `seen:`,
  `context:`) belongs to the sighting and should move; `title:unresolved`
  describes the duplicate's own title and should not. It sticks, too:
  `title_is_unresolved()` trusts the tag over the title in front of it, so one
  bad carry marks a healthy item as junk permanently.

- **An indented code block is not a citation, and there is now a test saying so.**
  This is how six of this repo's test fixtures reached the live library on
  2026-08-22: an audit message demonstrated malformed CommonMark inside a
  four-space indent, and the pre-AST scanner had no notion of an indented block —
  its comment said as much, reasoning that an indented line is usually a list
  item. The stray backtick then left the URL exposed as prose. The AST fixed this
  in 0.11.0 as a side effect and nothing has been captured that way since; the
  test pins it shut. The companion test records the honest limit: unindented,
  the same fragment IS a citation, and the parser is right to take it.

## 0.11.3 — 2026-08-23

Cleaning up what the old tokenizer left in the library. The grammar fix stopped
new damage; these two passes deal with the 96 rows already there.

- **Repair now recovers a URL from an illegal tail.** Rows exist ending in a
  shell backslash, an unpadded table pipe, or an ANSI reset from pasted terminal
  output. RFC 3986 permits none of them unencoded, so the address ends where the
  tail begins and cutting there recovers it rather than inventing it. Four rows
  in the live index qualify, all as merges into the clean URL already present.

  It refuses the case that looks identical and is not: if URL text _resumes_
  after the illegal character, the run was one literal.
  `https://files.rcsb.org/download/{ID}.pdb` cut at `{` would manufacture
  `https://files.rcsb.org/download/` — a real, fetchable directory nobody
  cited. So would a cut that leaves no host at all. Both return "no repair".

  The blanket "a backslash means a regex" rule is gone with it. That was too
  broad: it also skipped `https://cloud.r-project.org\`, where the backslash is
  a shell line-continuation and the address in front of it is real.

- **A new retirement pass removes rows that can never be a source.**
  `scripts/retire_rows.py`, dry run by default. Repair corrects a URL that has
  a right answer; retirement removes one that has none, and the two deliberately
  do not share a predicate. 92 rows qualify: 31 API templates
  (`{locus}`, `${VERSION}`), and 61 addresses today's rules already refuse —
  fixture names, font CDNs, DNS-over-HTTPS endpoints, badge SVGs, image files —
  captured before those rules existed.

  The predicate is "can never resolve to a document", **not** "contains a
  character RFC 3986 forbids". The two overlap and are not the same test, and
  using the character test to decide deletion is how a real row eventually gets
  thrown away. Retirement also asks repair first: `https://cloud.r-project.org\`
  fails the address test, yet the citation behind it is recoverable, and judging
  it without asking would have trashed it for a reason that reads convincingly in
  a log.

  Trashing is `deleted: 1`, recoverable from any Zotero client, never the
  permanent DELETE.

## 0.11.2 — 2026-08-23

- **A URL cut short by a template is dropped, not stored as its prefix.** The
  0.11.1 whitelist changed how a template in plain prose fails, and not for the
  better: `https://files.rcsb.org/download/{ID}.pdb` used to be stored whole,
  where no exclusion rule caught it but it could never resolve, so it failed
  loudly as the URL-as-title junk this plugin removes. Stopping at `{` instead
  stored `https://files.rcsb.org/download/` — a real, fetchable directory that
  acquires a genuine title and reads as a citation nobody made. Quiet wrong data
  is worse than loud junk.

  A match is now discarded when URL text _resumes_ after the illegal character:
  `{` followed by `ID}.pdb` means the run was one literal. Whitespace never
  counts, since that is how a URL normally ends, and neither does a _closing_
  delimiter — a closer can only appear after the thing it closes, so the URL had
  already ended. That second rule is not decoration: without it,
  `[https://example.org/bar].` lost a real citation, because the match stops at
  `]` and the sentence period reads as resumed URL text. An existing test
  caught it.

  Measured over the same 1,370 real assistant messages: **0 messages change, 0
  URLs dropped**. Forcing the guard to fire on every bare URL drops 852 of the
  4,009, so the measurement can tell a difference when there is one.

  A regex literal still escapes both rules — CommonMark unescapes `\.`, so
  `https://data\.gramene\.org/...` arrives with no illegal character left and
  `*` is a legal sub-delimiter. Two such rows are in the live index. That is
  the wildcard class, which needs the code-block judgement rather than the
  grammar; `test_a_regex_literal_survives_because_commonmark_unescapes_it`
  records the limit instead of hiding it.

## 0.11.1 — 2026-08-23

- **The URL tokenizer asks the grammar instead of a list of exclusions.** The
  bare-URL matcher was a character blacklist, ``[^\s<>"'`\]]+``, so anything
  nobody had thought to exclude was taken as URL data: a trailing `|` from an
  unpadded table cell, `{ID}` from a template, a raw ANSI escape from pasted
  terminal output. Those addresses can never resolve a title, so they decay into
  the URL-as-title junk this plugin exists to remove — 49 such rows are in the
  live index. Lengthening `TRAILING_PUNCT` is the wrong layer: it consumes the
  bad boundary rather than preventing it, and only ever in trailing position.
  The character class is now derived from RFC 3986, with three departures, each
  argued in the source: `[` and `]` stay out (legal only in an IPv6 host,
  which has its own branch), `'` stays out (legal, but across 1,370 real
  messages all seven apostrophes adjacent to a URL were shell, Python or English
  delimiters and none was URL data), and non-ASCII is admitted per RFC 3987,
  because a strict-ASCII class truncates `.../wiki/München` to `.../wiki/M`
  — the same damage as the `]` truncation that once broke every IPv6 URL.

  Measured over 1,370 real assistant messages and the 4,009 URLs 0.11.0 extracts
  from them, the change is a **no-op: 0 messages differ, output byte-identical**.
  Every illegal character in that corpus already sat inside a code span or fence,
  which the AST skips, and the live index's bad rows all predate the 0.11.0
  deploy. This removes the mechanism, not a measured defect rate. A deliberately
  broken variant admitting a space changed 218 of the same messages, so the
  measurement could tell a difference when there was one to tell.

## 0.11.0 — 2026-08-22

A second external audit, this time of the 0.10.0 fixes themselves. Nine findings,
all reproduced locally before being accepted — plus one that neither the audit
nor I had ranked, which turned out to be the largest.

- **A parser now decides where a URL ends.** Extraction scanned raw text: a
  line-oriented fence state machine and hand-paired backtick runs. Measured
  against a CommonMark reference over 1,415 real assistant messages, that lost a
  citation on 0.14% of them but mangled the _boundary_ of **10.72%** of the URLs
  it found. Trailing punctuation was trimmed from markdown that had never been
  parsed, so `**[text](url)**` kept its emphasis — `*` is in no trim set, and by
  leaving the URL ending in `*` it also stopped the paren-balance rule from ever
  firing. Lengthening the trim set is not the fix: RFC 3986 makes `_` unreserved
  and `*` a sub-delimiter. Extraction now walks a markdown-it AST — skipping code
  spans, fences and inline HTML, taking a link destination exactly as the parser
  reports it, suppressing the label inside a link, and running the bare-URL
  matcher only over text the parser has already stripped of markdown.
  Re-measured on the same corpus: boundary damage **10.72% → 0.09%**, structural
  loss **0.14% → 0.00%**.

- **116 damaged rows repaired in place.** A URL ending in `**` can never resolve
  a title, so it degrades into exactly the URL-as-title junk this plugin exists
  to remove. `scripts/repair_urls.py` corrects them, merging into the existing
  item where one is already present and carrying the duplicate's tags across
  before retiring it to the trash. It deliberately refuses 17 rows that are not
  URLs at all — regexes, wildcards and escape sequences the old extractor
  captured — because "correcting" those would fabricate an address nobody cited.

- **Queries are no longer rewritten.** Tracking params were dropped by
  `parse_qsl` + `urlencode`, a round trip that does not preserve what it was not
  asked to change: a valueless field gained an `=`, percent-encoding was
  normalised, `+` was reinterpreted. Signed and opaque queries survived none of
  it. Filtering now splits on `&` and rejoins the survivors byte for byte. The
  blanket `&amp;` fold is gone too — it was at the wrong layer, since a literal
  `&amp;` is legal URL data and only the parser knows how the URL was written.

- **A claim is now answerable rather than guessed at.** A POST that raised was
  treated as a POST that created nothing, so the claim was released and the next
  sighting posted a duplicate — but Zotero can commit before the response is
  lost. The item key is now chosen before the request goes out and stored with
  the claim, so an abandoned claim is settled with one GET: item there, complete
  it; item absent, release and retry. Claims younger than 60s are left alone.

- **A hook killed by its own timeout releases its claim.** `timeout 10` sends
  SIGTERM, which Python leaves at its default disposition — the process died
  without unwinding, stranding the URL as both uncaptured and undedupable.

- **The loser of a race keeps its provenance.** It queues its context and project
  tags for whoever completes the item, instead of dropping the sighting silently.

- **Fetches connect to the address that was checked.** The guard resolved a name,
  then handed the _name_ onward, so the inner transport resolved it again —
  letting an attacker-controlled DNS answer public for the check and private for
  the connect. The validated address is now pinned for the request, with the Host
  header and TLS SNI preserved so virtual hosts and certificate verification
  still work, and restored afterwards so each redirect hop gets its own check.

- **A report cannot cite itself.** Item titles come from fetched pages, and the
  commonest junk title here is a bare URL — bolded as prose, those were read
  straight back as citations. Titles are now shown as literals in a code span
  built so nothing in the content can close it. The no-capture marker is no
  longer an unauthenticated kill switch: it is honoured only in assistant output,
  and only when it leads the message.

- **A resolved title survives a 412 retry.** The resolver was re-invoked per
  attempt, so a title found on the first try was discarded if the second failed.

## 0.10.0 — 2026-08-22

The rest of the external audit of 0.9.0.

- **The Stop hook never read "the last message."** It selected every assistant
  event in the whole transcript, concatenated them, and kept the last 200 lines.
  Old URLs were therefore re-captured on later turns with today's `seen:` tag and
  the current turn's `context:`, and a code fence opened in an earlier turn
  decided whether this turn's citations were captured at all. It now uses the
  `last_assistant_message` the hook is given, falling back to the final assistant
  event only — base64-framed so a multi-line message survives intact.

- **`/source-delta` re-captured everything it reported.** It emitted markdown
  links, the one form capture deliberately keeps, so displaying the report
  stamped today's `seen:` tag onto the very sources it listed as dropped. Reports
  now show URLs in backticks and carry an explicit no-capture marker that capture
  honours. Two independent layers, because a reformatted report loses its
  backticks and a summarised one loses its marker.

- **The fence and code-span rules were not CommonMark.** Any three-backtick or
  three-tilde line toggled state, so `~~~` closed a `fence and` closed a
  ```` one; only single-backtick same-line spans were understood, so ``` ``url` ```` `
  slipped through. Closing fences now require the opener's character, at least
  its length, and nothing but trailing whitespace; spans pair runs of equal
  length and may cross lines within a paragraph. Blockquoted fences are
  recognised. Span pairing is bounded to a paragraph so one stray backtick cannot
  silently swallow every later citation. Retention on a 324-message replay is
  unchanged at 95.6%.

- **Host filtering was not an SSRF boundary.** It inspected spelling only, so
  `2130706433`, `0x7f000001`, `017700000001` and `127.1` — all 127.0.0.1 to any
  HTTP client — passed straight through, and a public host could simply redirect
  to a private one. Fetching now goes through a guarded transport that resolves
  and validates every hop, redirects included. Known limit, stated rather than
  hidden: validation happens at resolve time, so DNS rebinding is not covered.

- **Dedup was neither atomic nor durable.** `lookup → POST → INSERT OR IGNORE`
  let two sessions both create an item, after which one key was silently dropped
  and the other Zotero item became invisible to dedup forever. A URL is now
  claimed before the POST; a failed POST releases the claim. Verified with real
  concurrent processes, not a simulated race.

- **A 412 lost this session's tags.** Two sessions tagging one item is ordinary,
  and the loser simply raised — with no retry queue behind it, the tags were
  gone. It now refetches and reapplies, merging the other writer's tags instead
  of overwriting them, and gives up loudly after three attempts.

- **`prune` skipped items while paginating.** Trashed items leave Zotero's
  listings, so removing entries from one page shifted the rest left and the next
  offset stepped over exactly as many as had been removed — reporting a clean
  sweep over a collection it had only partly seen. It now snapshots before
  writing. The old test could not catch this because its mock never removed
  patched items.

- **IPv6 was broken end to end.** The tokenizer stopped at `]`, truncating every
  bracketed URL, and canonicalization rebuilt the netloc without brackets. The
  passing IPv6 tests only ever called `is_excluded` directly.

## 0.9.1 — 2026-08-22

Fixes from an external audit of 0.9.0.

- **Decoding the full HTML entity table rewrote the URL's structure.** 0.9.0 ran
  `html.unescape()` over the whole URL, but that table contains the URL's own
  delimiters: `&sol;` is `/`, `&num;` is `#`, `&quest;` is `?`. So a path
  segment could forge a separator, or invent a fragment that canonicalization
  then discarded — storing a different resource than the one cited. Only `&amp;`
  is decoded now. It is the one entity a URL acquires merely by being written
  into HTML, it cannot move a delimiter, and it is what the dedup fold actually
  needed: a re-printed `&quot;` comes back as `&amp;quot;`.

- **`pyproject.toml` still advertised 0.1.0** — eight releases stale, while
  `test_version.py` asserted "exactly one source of truth" in its own docstring.
  The drift guard now covers it.

- **`pytest -q` reached the internet** despite the README promising it did not.
  The live title-fetch tests carry their own fixture and never checked an opt-in.
  `addopts = -m "not live"` makes the documented behaviour the real one.

## 0.9.0 — 2026-08-22

- **A URL that is shown is no longer captured as one that is cited.** The hook
  reads Claude's own output, so auditing the library re-captured the URLs the
  audit printed. Worse, printing re-escapes them (`&quot;` → `&amp;quot;`), which
  is a _different string_ — it missed the dedup lookup and created a new item
  every pass, with no ceiling. `extract_urls` now skips fenced code blocks and
  inline code spans, which is Markdown's own way of marking a literal.

  Measured over 324 sessions' messages, 95.6% of captured URLs are unaffected;
  what drops out is mostly internal infrastructure. Indentation is deliberately
  not a signal, because an indented line is usually a list item. Verified by
  replaying the real transcripts that caused the incident, not only fixtures —
  a synthetic-only check had already led to the wrong conclusion here once, when
  fenced blocks turned out to carry 1 of 8 occurrences and inline spans 7.

- **HTML entities in a URL are decoded before storing.** A link lifted out of
  rendered markup carries the page's escaping, so `?a=1&amp;b=2` used to become a
  second item for a source already held. This is what made the loop above
  unbounded rather than merely repetitive, and it is a real defect on its own.

## 0.8.1 — 2026-08-21

- **Repairing a URL no longer strands the item.** `title_is_unresolved` detects a
  failed fetch by `title == url`. Moving the URL while leaving the old URL as the
  title breaks that equality, so the item silently stops looking unresolved and
  no backfill revisits it again — the same latch the re-enrichment work removed,
  reintroduced from the other side. `update_url` now carries a URL-shaped title
  along with the URL. A real title is metadata and is left untouched.

## 0.8.0 — 2026-08-21

- **A host that is not a hostname is no longer captured.** A display ellipsis
  reached the library as `https://…` and then raised "Invalid IDNA hostname" on
  every fetch attempt for the rest of its life. The test is IDNA encoding — what
  the HTTP client itself applies — rather than an ASCII whitelist, so genuine
  internationalised domains (`münchen.de`, `例え.テスト`) are kept. IP addresses
  are settled before the name test, which is what keeps public IPv6 working.

## 0.7.0 — 2026-08-21

- **Preprint DOIs were being built wrong.** The path regex swallowed the version
  suffix, so `10.1101/2025.05.30.656746v1` went to doi.org — not a DOI, and
  correctly a 404. The earlier audit blamed unregistered preprint DOIs; that was
  wrong. Stripping the suffix resolves the same papers 200. bioRxiv's own
  `api.biorxiv.org/details` answers 200 with an empty body, so it is not the fix
  the audit assumed.
- **The DOI prefix is no longer hardcoded.** bioRxiv minted `10.64898` for 2026
  papers alongside `10.1101`; matching only the latter silently skipped them.
- **Titles are normalised.** Publisher CSL metadata is typeset, not plain text —
  titles arrived carrying `<sup>`/`<i>` markup, HTML entities and the newlines of
  the source XML, and were stored in Zotero verbatim. Normalisation happens once
  at `fetch_title`'s boundary so a new resolver cannot forget it. A bare `<` is
  left alone: "Cost < 5% of baseline" is a real title, not markup.

## 0.6.0 — 2026-08-21

- **`backfill_titles.py --prune`** sweeps the exclusion rules back over items
  captured before those rules existed. `is_excluded` stays the single definition
  of what is not a source, so a rule added later needs no second list.
  Items are moved to the Zotero **trash**, not deleted — the collection is a
  provenance record, so a bulk cleanup has to be reversible. Pair with
  `--dry-run` first; it prints every URL it would remove.
- **A bulk pass no longer gives up on one slow response.** Paging the collection
  retries a timed-out page and raises after three attempts rather than returning
  early — a truncated sweep is indistinguishable from a short collection to the
  caller, so it must fail loud. Maintenance passes also get a 30s Zotero timeout
  instead of the hook's interactive 5s.

## 0.5.0 — 2026-08-21

- **Give the version one source of truth.** Four copies disagreed —
  `plugin.json` at 0.4.0, the package `__version__` at 0.1.0, and three
  User-Agent strings at 0.4, 0.1 and 0.1. Version and UA are now defined once in
  the package, and a test fails the build when they drift from the manifest.
- **Drop names the standards reserve.** `example.com`/`.net`/`.org` and the
  reserved TLDs (`.test`, `.example`, `.invalid`, `.localhost`, `.local`,
  `.onion`, `.alt`, `.arpa`, `.internal`) can never be a real source, and they
  are exactly what test fixtures use — five rows in the library came from
  another project's security fixtures being echoed into a session. Matching is
  on label boundaries, so `myexample.com` and `example.com.evil.co` survive.
- Test fixtures moved off `example.com` (and the end-to-end fixture off
  `.invalid`) so the suite still exercises the capture path it is testing.

## 0.4.0 — 2026-08-21

Backfilled entry. This release is about what the capture path should _never_
have stored, plus recovering what it stored badly.

- **Stop capturing page assets and infrastructure as sources.** Font CDNs,
  DNS-over-HTTPS endpoints and analytics beacons can never resolve to a title,
  so they accumulated forever. Asset extensions (`.css`, `.js`, `.woff`,
  images, `.map`) are excluded by path, with carve-outs for paths that are
  genuinely pages — GitHub `/blob/` and `/tree/` views, workflow badges,
  Wikimedia `/wiki/File:` description pages.
- **Send a contactable User-Agent.** Wikimedia's policy rejects a UA with no
  way to reach the operator; both `zotero-provenance/0.1` and a plain
  `Mozilla/5.0` got 403 from en.wikipedia.org. The UA now carries the project
  URL as its contact point. Recovered 49 items.
- **Stop truncating URLs that contain a closing paren.** `URL_RE` excluded `)`
  from the URL character class, so a match ended at the first one — corrupting
  Cell Press PII links and the DOIs behind them
  (`10.1016/s0092-8674(00)80876-3`) and Wikipedia disambiguation pages. The
  balanced-paren rule that exists to judge a trailing paren could never see
  one. Affected 31 items, all unresolved; several had been misattributed to
  publisher WAFs.
- **`--host` filter on the backfill pass**, so a fix that helps one host costs
  one host's requests instead of re-fetching a mostly-unfetchable backlog.

Known gap: the 31 already-corrupted URLs are not recoverable from the library.
The truncated tail was never stored.

## 0.3.0 — 2026-08-21

- Resolve DOIs by content negotiation instead of scraping.
- Resolve arXiv, PubMed, bioRxiv and GitHub through their own APIs.
- Add a backfill pass for items captured before re-enrichment existed.

## 0.2.0 — 2026-08-21

- Re-enrich unresolved titles instead of latching them forever.

## 0.1.0 — 2026-08-20

- Initial release: capture hook, commands, end-to-end tests, README.
