# Changelog

## 0.59.0 — 2026-09-06

- **A scoped run never reached the site root.** `--only-host example.com`
  matched `https://example.com/%` and `https://%.example.com/%`, but
  `canonicalize` strips a bare `/`, so a site root has an EMPTY path and
  matched nothing. Measured with the real `_host_boundary` over every
  site-root row in the live index:

  | site-root rows | reachable by `--only-host <own host>`, old | new |
  |---|---|---|
  | 191 of 5,133 | 0 | 191 |

  The report still called the run scoped and complete. Fixed at the one
  function: after the host the URL may end, or continue with `/` or `?`
  (a fragment never survives canonicalize); six LIKE patterns per host
  level, because `host%` alone would take `example.com.evil.test`, the
  suffix the boundary exists to refuse. The boundary tests still pin
  `notexample.com` and that suffix as controls.

- **`--limit -1` meant the whole corpus.** SQLite reads a negative LIMIT as
  no limit; `repair_urls` and `retire_rows` guarded it, `snapshot_pages` and
  `drain_queue` did not, so the value someone types to be careful started a
  multi-hour run. Refused ONCE where the SQL is built (`_limit_clause`,
  three call sites) so no CLI has to remember, and again at the two CLIs for
  the message. `verify_dois --limit 0` used `if args.limit:` and checked
  every DOI; `is not None` now, the `--limit 0` class fixed a third time.

- **`retire_rows` reported the rows the limit held back as policy
  exclusions.** `held` was computed after `--limit` truncated the plan, so
  `--limit 1` over 3 hard + 1 policy printed "3 more are policy exclusions".
  Counted against the plan now; the slice is a claim about this run only.

  Mutations, all caught first round: site-root patterns dropped; boundary
  loosened to `host%`; the negative-limit raise removed; the CLI guard
  removed (sqlite still refuses, and the CLI test asks for the message);
  held computed after the slice.

  Audit findings M1, M2, L2 of `audit_whole_repo_2026-09-06.md`.

## 0.58.0 — 2026-09-06

- **A hook timeout left no record, and a POST it cut off was never revisited.**
  `HookTerminated` derives from `BaseException` on purpose, so the per-URL
  loop cannot swallow it -- and `main()` caught only `Exception`, so it
  escaped as a raw traceback into `capture.log`. Health read those lines as
  "the last N lines are unreadable", and the next successful capture erased
  the finding. The live log held two of them. Worse: when the timeout landed
  after the POST had gone out, the claim was correctly kept (Zotero may have
  committed) but nothing would ever revisit it, because `_resolve_claim` runs
  only when the URL is cited AGAIN. Two live rows (pending `IR5PD8S6`,
  claimed 08-30; `8LUQ8R4J`, 09-01) sat that way for five and six days,
  listed by `verify_index` under "claims still in flight".

  Fixed at the layer that raises: `main()` records `hook-terminated` as a
  structured event, and health counts it as a refusal (a fact about us).
  A POST cut off after `issued = True` is queued for retry; a drain replays
  it through the reservation, and `_resolve_claim` settles it by asking
  Zotero for the pending key -- measured on the first test written, with no
  second POST (the positive control against duplicates).

  Found while writing that test: a drain that ran INSIDE the 60-second claim
  window read a deferral ("held by another session") as a clean run and
  dequeued the only record that anything was owed. `CaptureResult` now
  carries `urls_deferred`, and a drain leaves such an entry alone without
  counting an attempt.

  `verify_index` prints each unfinished claim's age: `claimed 12s ago; in
  flight` or `claimed 6d ago; cut off -- drain_queue settles it`.

  Mutations, all caught first round: the timeout uncaught in `main`; the
  event dropped from `REFUSAL_EVENTS`; the cut-off POST not queued; the drain
  dequeuing a deferred entry; the age note saying "in flight" forever.

  Audit finding H1 of `audit_whole_repo_2026-09-06.md`.

## 0.57.0 — 2026-09-05

- **A refusal was hashed as the document, and then reported as an intact
  source.** 26 rows in the live index store `sha256("")` as their
  `content_hash`, and 25 of them verified `unchanged` on 09-05. Probed all 26
  read-only through the production client, with a control that returned 9,083
  bytes:

  | what answered | n |
  |---|---|
  | HTTP 202, `Content-Length: 0`, CloudFront | 18 |
  | HTTP 202, `Content-Length: 0`, awselb/2.0 | 6 |
  | HTTP 200, `Content-Length: 0` (a REAL empty response) | 1 |
  | HTTP 200, 168,811 bytes (the page came back) | 1 |

  Who actually answered: ieeexplore (7), figshare (4), sketchfab (3),
  morningstar (3), dataverse.harvard.edu (2), semanticscholar (2),
  degruyterbrill, cgtrader, jove. **Six of the nine `doi.org` rows resolve onto
  ieeexplore**, which is only visible because the responding address is now
  recorded — all 26 rows predate 0.38.0 and carry an empty `final_url`.

  Root cause: `hash_page` asked `raise_for_status()` — *did the request fail?* —
  and never asked *did we receive a document?*. 202 is a SUCCESS status, so a
  response carrying nothing passed every gate, was hashed, and became the
  provenance record; `verify` then re-read the same 202, found the digest
  matched, and concluded `unchanged`. **Worse than the `unreachable` and `gone`
  defects that preceded it: those mislabelled a failure, this reported a success
  we never had.**

- **The fix is on the STATUS, not on the emptiness.** 202/204/205 carry no
  representation by definition (RFC 9110), whatever the body length turns out to
  be. Refusing to hash an empty body was the rejected alternative and it points
  the wrong way in both directions: a 202 that ships a challenge body would
  still be hashed, and `biodiversitylibrary.org/api3` really does answer 200
  with zero bytes — a true fact this tool exists to keep. Both directions are
  pinned by tests, and the mutation that implements the band-aid is caught by
  the positive control.

- **`no_content`, not `blocked`.** `NotTheResource` routes a challenge page to
  `blocked` and "refused; the page may be perfectly fine" fits a bot wall
  exactly — but a 204 refused nothing, and calling it a refusal asserts a fact
  about the host that never happened. One word covering both findings is how
  `unreachable` came to mean gone AND blocked, which has cost this project a
  release twice.

- Mutation-tested 6/6 caught on the first round, including "only 202 counts",
  "blame the address we asked rather than the one that answered", and the
  band-aid above. Real execution against the hosts that produced the finding:
  the `doi.org` row is refused and names **ieeexplore** as the answering host,
  the genuinely empty 200 still hashes, and the control hashes 9,083 bytes.
  1,169 tests.

- **Blast radius is a LOWER BOUND, not a count.** Response status is not stored,
  so a 202 that carried a challenge body is indistinguishable in the index from
  a real document. 26 is what this signature can see, not what happened.

- **KNOWN, deliberately not fixed here:** `page_is_visible` counts anything that
  is not a clean 404/410 as visible, so a parent behind a 202 wall reads as
  *seen* and can corroborate a `gone` on its leaf. Changing it would make `gone`
  strictly harder to claim, which is the right direction — but the last rule
  proposed for that function was killed by listing the rows it would touch
  first, and that measurement has not been made.

## 0.56.0 — 2026-09-05

- **A `changed` verdict could not tell a view counter from a rewrite.** 714
  rows carry `changed`, and **710 of them have no stable digest at all**: they
  read identically twice 2s apart, so `verify` never enters the stable path and
  the verdict rests entirely on a whole-document hash taken weeks earlier.

  Probed by reading 14 of them as two pairs ~20 minutes apart, driving the
  production chunker and `stable_digest` rather than a replica, with a pinned
  immutable blob as positive control (byte-identical throughout).

  | bucket | n |
  |---|---|
  | agrees within its 2s pair, DIFFERS over 20 min | 4 |
  | steady over 20 min | 9 |
  | volatile within 2s today | 2 |

  What moved, from diffing the bodies rather than trusting the digests: a CSRF
  token whose TTL sits between 2s and 20 min (4help.vt.edu), ad cache-busters
  `c=-303483026` -> `c=1158063100` (nature.com), a redeploy id
  (code.claude.com), and `Views: 308` -> `Views: 310` (doi.org).

  > Agreement inside a 2-second pair rules out fast volatility and nothing
  > slower. A view counter is enough to manufacture "this citation changed".

- **The prediction was written down first and was WRONG.** Predicted >=8 of 14
  manufactured; measured **4** (Wilson 95% CI 6–49%). The majority held steady,
  so unlike `unstable` (47% = our method) and `stable_changed` (175 vs 2), this
  bucket largely SURVIVED the probe built to break it. Three prior findings of
  one shape do not make a fourth. **"Steady over 20 minutes" is not "the
  difference was real"** — it bounds the claim by the interval and nothing more.

- **A sketch is now stored BESIDE `content_hash`, and the hash keeps its exact
  sensitivity.** New columns `content_sketch` / `content_sketch_algo` hold a
  bottom-k sketch of every chunk of the SAME read that produced the hash, so a
  mismatch can be given a size. The rejected alternative was to suppress the
  verdict when overlap is high: that trades a visible false positive for a
  silent false negative, and for a link-rot tool the silent one is worse.

- **Its own tag, `cdc64+kmv128-full/1`.** The stable sketch samples the subset
  two reads AGREED on; this one samples every chunk of a single read. Same
  arithmetic, different populations — which is exactly what makes mixing them
  plausible and wrong, since comparing them answers a question about how we
  sampled. That is the defect 0.55.0 spent a release removing, so the two are
  made incomparable by declaration rather than by anyone remembering.

- **Backfill is legitimate in exactly one situation, and only there.** A sketch
  cannot be invented for the 3,813 existing rows from a later read: it would
  describe different bytes than the hash beside it. But when a fresh read still
  EQUALS `content_hash`, the bytes are proven identical, so a sketch cut now is
  a truthful sketch of what was hashed then. That equality is the whole licence.
  Rows that already differ can **never** acquire one — only a digest of those
  bytes was ever kept. Storing only a hash means you can never afterwards ask
  what changed.

- **`None` is a real answer and stays distinguishable from 0.0.** No sketch, a
  sketch cut by another method, or a read that recorded no units all yield "we
  cannot say", and the report prints that sentence instead of a number. Rendering
  an absent value as 0.0 would announce a total rewrite on the strength of our
  own gap — the shape of every finding this project has had to withdraw.

- **The report no longer says "of the document".** A sketch is 128 chunk
  digests, not the document. Live on 4help.vt.edu, 11 chunks differed and the
  sketch saw none of them, printing `1.000` beside `CHANGED` — two halves that
  contradict each other, inviting the reader to believe the friendlier one. It
  now names its resolution: `(no difference the sketch can resolve; a small edit
  hides here)`, or `(0.992 of the sampled chunks shared)`. The same run scored
  1.000 and then 0.992 on consecutive passes, which is the sampling variance
  made visible rather than hidden behind a confident number.

- **`set_content_hash` requires the sketch rather than defaulting it.** Its
  only sensible default is "none", so a caller that forgot would silently store
  an uncharacterisable hash, invisibly from both ends. This project shipped
  `--sleep` parsed and never passed for an entire release.

- **Mutation testing removed a second copy of one rule.** Six mutations, all
  caught — but the first run had two survivors, and both were informative. One
  was a badly built mutation that did not test what it was labelled. The other
  showed that the Python guard `not row["content_sketch"]` was **redundant with
  the SQL write-once WHERE clause**: deleting it changed no behaviour, which is
  what proved it was never the guard. Removed, so write-once lives in one place.
  A test seeding `algo=""` was also found to return at the tag check and never
  reach the empty-sketch branch — two tests exercising one path.

- Verified by real execution on a scratch index over the live network, seeded
  from bodies fetched 40 minutes earlier: a real change characterised, a
  backfill where bytes matched, and no backfill where they differed.

## 0.55.0 — 2026-09-04

- **The stable digest was reporting our own sampling as drift.** Measured
  against the live index, `stable_changed` stood at **175 against
  `stable_unchanged` 2** — a corpus of published papers cannot be 99% drifted,
  and 41 of the 175 were nature.com ARTICLES. The shape was the finding before
  any probe ran.

  The proof it was ours: a nature.com article read twice, 120 seconds apart.
  The two bodies differed in **32 byte positions** and they were one CSRF
  token; the article was byte-identical. Yet the agreed list held 3,475 chunks
  in one round and 3,429 in the next, so the two hashes covered **different
  regions of the page**.

  > A stable digest's domain is chosen by the pair of reads that produced it,
  > but the comparison treated that domain as fixed. Two hashes over different
  > byte sets are not comparable, so `stable_changed` was not a claim about the
  > source.

  This is the same shape as two defects already fixed here — comparing a 5 MiB
  prefix against a 32 MiB one, and re-reading a truncated row over the wrong
  span. Both were caught by insisting a comparison cover the same thing. This
  one survived because the span is **derived rather than configured**, so no
  parameter looked wrong.

- **The comparison is now overlap, not equality.** `sketch_of` builds a
  bottom-k (KMV) sketch of the agreed chunk SET — order-free, repeat-free, and
  bounded at ~2 KB however large the document — and `stable_similarity`
  estimates Jaccard between two of them. A page that gained 46 volatile chunks
  reads 98.7% similar instead of "CHANGED". The domain is allowed to move,
  because it always did.

- **The threshold is measured, not chosen.** Over 12 live pages:

  | population | range |
  |---|---|
  | same document, two pairs 120s apart | 0.958 – 1.000 |
  | two different articles, same host | 0.239 – 0.595 |

  Nothing lies between 0.595 and 0.958. `MIN_STABLE_SIMILARITY = 0.80` sits
  near the middle of that gap rather than against either edge, and a test
  asserts `0.59479 < MIN < 0.95824` so a value outside the measured range fails.

- **The cost is stated rather than tuned away.** From the same run: rewriting
  100 bytes of a 271 KB article moves similarity to 0.998, and 1,000 bytes to
  0.990 — both ABOVE the 0.958 floor that unchanged pages occupy. **A small
  edit is invisible and no threshold recovers it**, because the noise from
  moving domains is larger than the edit. 10,000 bytes reads 0.90 and 50,000
  reads 0.68. What this detects is a page becoming substantially different.
  Less was lost than it appears: the equality test reported `changed` for
  unchanged pages too, so it never distinguished a small edit from noise
  either — it said "changed" always and was right by accident.

- **Two of the suite's own tests were passing for the wrong reason.** They
  asserted that a one-paragraph edit reports `stable_changed`, under a
  comparison that answered "different" for every pair of reads. A test asserting
  "changed" against a method that says changed for everything cannot fail. Their
  fixture now makes a change the method actually claims to detect, and the edit
  it can no longer see has its own test recording the limit.

- **A surviving mutation found a guard nothing could feed.** Making an empty
  sketch return an empty set instead of `None` broke no test — `verify` cannot
  reach that branch, because a row holding no sketch takes the baseline path.
  The 0.22.0 audit named this class twice. Now driven directly, because an
  empty set would compare as total agreement with another empty one.

- **Existing rows re-baseline themselves.** The format changed, so the tag did:
  `cdc64/1` → `cdc64+kmv128/1`. Every stored value is now incomparable by
  declaration and re-baselines through the path that already exists, which is
  the mechanism `stable_algo` was added for. No migration, no schema change —
  the sketch lives in the `stable_digest` column, whose format that tag
  declares. The 879 rows re-baselined earlier today onto `cdc64/1` are re-cut
  again; that cost is real and is the price of the tag doing its job.

- Verified by real execution, not fixtures: nature, springer and github — three
  pages among the 175 — driven through the shipped `verify` twice on a temp
  index, over the live network. Pass 1 recorded baselines, pass 2 returned
  `stable_unchanged` for all three.

## 0.54.0 — 2026-09-03

- **A login page was becoming the name of a cited work.** 0.51.0 refuses a
  document that *declares* it is not the resource — a reCAPTCHA interstitial
  sets a cross-site `<base>` — and that rule is untouched. It is also narrow:
  measured against the live collection, 100 items carry a title naming an
  access barrier rather than the source, and the `<base>` rule catches 10 of
  them. The other 90 declare nothing. They are Imperva's "Client Challenge" on
  46 pypi package pages, "Sign in to GitHub", "Tailscale", "Log in · PyPI" —
  ordinary walls served with HTTP 200.

  `fetch_title` now also refuses a document that carries too little prose to be
  a resource. A gate is short because it has nothing to say.

- **The rule that was tried first is recorded because it was REFUTED**, so it
  is not proposed again. A corpus rule — one title shared by URLs with no
  common path ancestor — scored **precision 0.12** over all 144 duplicate-title
  groups (TP=7, FP=52, FN=2). Its false positives are *one paper cited at
  several addresses*: doi.org and the publisher, arXiv `/html` and `/pdf`. That
  is the case a citation index exists to serve. No tuning helps: "many
  addresses, one title" is produced identically by a legitimate multi-address
  work and by a gate, so the corpus statistics are the same object. The
  separating information is in the response, which the title path was throwing
  away.

- **Two conditions, both load-bearing: a COMPLETE document, under 64 KB,
  carrying under 800 characters of prose.** The first draft claimed prose alone
  separated the populations, and a control sample refuted it — 19 of 33 real
  pages sampled from successfully-hashed rows carry under 800 characters,
  because GitHub issues and pull requests (351–689) and a Nature article (274)
  render their content in JavaScript. They are safe because they are 236 KB to
  5.5 MB and are never read whole, so no verdict is formed. Size alone does not
  separate them either: bioconductor serves a real package page in 29 KB —
  smaller than the 51 KB Hugging Face wall — carrying 4,832 characters.

- **A verdict is pronounced only on a document read whole**, which is the guard
  rail rather than a detail. A slow article cut off at the byte budget has
  little prose *so far* and would be condemned for being slow; the error would
  be a function of network speed. Truncated means no opinion, so the title
  survives. The read no longer stops at `</title>`, but it stops at 64 KB once
  the title is in hand — past the largest gate measured, so the extra bytes
  cannot change the answer.

- **False positives measured before the rule was written, not assumed**: 0 of
  the 2 real pages a 45-URL control sample would actually judge (the third
  judged page was a Reddit shell, correctly refused). n=2 is a weak bound and
  is stated as one, in the manner of the 0/40 bound the `<base>` rule carries.

- **Scope is deliberately narrow: the title path only.** `hash_page` is
  unchanged, so 0.51.0's stated limit — a same-origin login wall stays
  invisible to the *hasher* — still holds. Overturning it for titles is a
  judgement and the argument is recorded: for the hasher, the login page
  genuinely is what that address serves an anonymous requester, and recording
  its bytes is a true statement about our view. Storing "Tailscale" as a
  citation's *name* is a false one about the source, and it is worse than
  storing nothing, because a confident title clears `title:unresolved` and
  nothing revisits the item again.

- **Two of my own errors, both caught by mutation testing and both recorded.**
  The first version of the strip list was vacuous — BeautifulSoup already omits
  `<script>`, `<style>` and `<template>` from `get_text`, so emptying the list
  changed nothing and the mutation survived. `<noscript>` is the one it counts,
  and it is the one that matters: a shell's "You need to enable JavaScript"
  block is prose no reader ever sees, and counting it would lift a shell over
  the threshold. The second was the calibration bound, drawn from four control
  pages and wrong; it is corrected above from a 45-URL sample.

- Seven existing title-fetcher fixtures were bare documents — a `<title>` and
  no body — which under this release assert, accidentally, that a content-free
  document yields a title. They now carry ordinary page prose. Every assertion
  about the extracted title is unchanged.

## 0.53.1 — 2026-09-03

- **The guard added in 0.53.0 could not see a single one of the 1,217 rows it
  existed for.** It was written
  `if row["stable_algo"] and row["stable_algo"] != STABLE_ALGO:` — and `''` is
  the tag every pre-0.53.0 row carries, because it is the column's default. The
  leading truthiness check made the branch unreachable for exactly the
  population it was built to protect.

  Found on the first live run against those rows, five minutes after 0.53.0
  deployed: five GitHub gists and repositories came back **DOCUMENT CHANGED** —
  the tool stating that five cited sources had drifted when the only thing that
  had moved was our own unit of comparison. That is the precise failure
  `stable_algo` was introduced to prevent, shipped inside the fix for it.

- **The fixture is what hid it.** The 0.53.0 test seeded `algo="lines/0"`, a
  value production has never held and never will; against a non-empty tag the
  broken guard behaves correctly. The test now seeds `""`, the real default, and
  fails against the 0.53.0 guard with the exact production symptom
  (`stable_changed` where `stable_rebaselined` belongs). This is the third time
  this project has shipped a bug that a fixture's convenient value concealed —
  after the frozen clock in `snapshot()` and `init_db` in the maintenance CLIs.

- **"Has a tag" and "has a DIFFERENT tag" are not the same question**, and only
  the second is about comparability. The check is now
  `row["stable_algo"] != STABLE_ALGO`, any value including empty — with the
  has-a-digest branch moved ahead of it, because a row that was never
  characterised also carries an empty tag and that is a first baseline, not a
  re-cut. The ordering has its own test; both branches live on the empty string
  and only the digest tells them apart.

- The five misreported rows never had their stored digest overwritten — the
  `stable_changed` branch does not write — so they were repaired by re-verifying
  them, and they now read `stable_rebaselined`.

## 0.53.0 — 2026-09-03

- **The unit of comparison was the line, and a line's length belongs to the
  source's formatter.** `stable_digest` compares two reads of a page and hashes
  what they agree on, so a per-request token stops reading as provenance drift.
  It aligned lines — so a unit differing in 9 bytes forfeited all 105,082 of
  them, and *the source decided* how much one nonce could cost us.

  Measured live on 2026-09-03, fetching 38 index rows twice three seconds apart
  and scoring against what actually differs:

      huggingface.co/datasets/google/frames-benchmark   ONE line of 630 KB,
                                                        0.05% truly different
                                                        -> coverage 0.65, refused
      link.springer.com/article/10.1186/s13059-...      81 per-render anchor ids
                                                        ~9 bytes each = 0.12%
                                                        -> coverage 0.70, refused

  Springer stamps a per-render number into every reference anchor
  (`id="ref-link-section-d10994812e529"` vs `d1246762e529`); because those sit
  inside one 105,082-byte line, **111,300 bytes were forfeited for 729 that had
  moved**. `stable_digest`'s own docstring already named "a per-render element
  id" as the thing it existed to derive away. The intent was right; the
  granularity defeated it.

- **The unit is now a content-defined chunk, cut on a rolling hash of the bytes
  themselves** — how rsync, borg and git packfiles compare two versions of a
  stream. Over the 38 pairs the line unit refused 21 documents it could have
  characterised; the new unit refuses 1, a genuine borderline at 3.95% truly
  different, and wrongly accepts none.

- **Smaller units are not the fix; re-synchronising boundaries are.** Fixed
  512-byte blocks were run as a control: an edit that changes a document's
  length shifts every later block and they never re-align — one page fell from
  0.9966 coverage to 0.1881, another scored 0.2885 where content-defined chunks
  scored 0.9188. Across the corpus fixed blocks rescued 2 rows and broke 2;
  content-defined chunks rescued 20 and broke none. That control is kept as a
  test, so "we made the units smaller" cannot become the remembered account of
  this fix.

- **`MIN_STABLE_COVERAGE` did not move, and that is the evidence.** Everything
  characterisable scores 0.92 or better and everything genuinely volatile 0.81
  or worse — a yahoo news page rebuilt 22% of itself between two reads seconds
  apart. The floor still sits in the gap it was chosen for. Changing the
  threshold was the band-aid hypothesis and the measurement ruled it out: no
  threshold value separates the populations while the unit is the line.

- **`stable_algo` records WHICH method produced a stored digest.** Without it
  this release would have found 1,217 stored digests mismatching at once and
  reported every one of those sources as having drifted — the tool
  manufacturing the exact class of finding it exists to report truthfully, which
  this project has already shipped and fixed for `unreachable`, `gone` and
  `blocked`. A row whose stored tag is not the current one is **re-baselined**
  and counted under its own name, never compared. Mutation-tested: removing the
  tag check makes the row come out `stable_changed`, which is the failure being
  guarded against.

- The write-once rule on `stable_digest` is widened by exactly one clause and
  not weakened: a digest cut by a *different* method was never evidence about
  this one, so holding it in place could only preserve a false comparison.

- **A count I wrote from arithmetic instead of a query was wrong.** The first
  draft of this entry said ~2,400 rows would re-baseline, reached by adding
  `stable_baseline` (1,217) to `unchanged` (1,179). `unchanged` means the
  whole-response digest matched, so those rows never reach the stable path and
  carry no stable digest at all. The real figure is 1,217 — every row that has
  one. Caught by running `select count(*) ... where stable_digest <> ''` before
  the sweep rather than after, which is the only reason it is not in the
  release note as a fact.

- **A negative control found my own framing wrong.** The pages I first selected
  as "app shells that must stay unstable" were chosen from rows recorded
  `unstable` — the very verdict under suspicion. Their near-zero line coverage
  was the same minified-single-line artefact, not volatility. Circular. The real
  control is a page that genuinely rebuilds itself, and it holds: yahoo stays
  refused at 0.7485. Separately checked that no two distinct URLs collide on a
  stable digest, including four claude.ai artifacts — the boilerplate-only
  hazard does not materialise.

## 0.52.0 — 2026-09-02

- **54 items in the library are titled "Checking your browser - reCAPTCHA".**
  Same defect as 0.51.0, in the CAPTURE path, with a larger blast radius and a
  longer history: dated 2026-05-28 through 2026-09-02 — one created the day this
  was found, so it was still happening — across roughly fifteen projects, on
  `pmc.ncbi.nlm.nih.gov` and `www.ncbi.nlm.nih.gov`.

  That string is not a weak title or a missing one. It is a false statement
  about the cited source, sitting in the field a reader trusts most, and it is
  worse than the hash defect because a title is what a human actually reads.

- **The sentinel is the URL, which is honest.** `fetch_title` already returns
  the URL for "could not get a title", and that answer says we failed and leaves
  `title:unresolved` set so something revisits the item. A confidently wrong
  title clears that flag forever, which is why "Real Tit" was fixed the same way
  and for the same reason.

- **The rule now has exactly one definition.** `document_disowns` moved to
  `url_processing`, which both `snapshot` and `title_fetcher` already import and
  which imports neither. Two call sites needing one rule is the precise shape
  that produced the User-Agent defect, where a consolidated value was
  re-hardcoded in a new module six days after it was consolidated. The guard
  derives its obligation by scanning every module in the package for the
  pattern, so it fails on a copy in a file nobody has written yet.

- **A vacuous pass, caught by its positive control.** The first version of the
  challenge-title test PASSED against ungated code. The mock served no
  `content-type`, `fetch_title` refuses a non-HTML response before reading
  anything, and so it returned the URL sentinel for the challenge and the real
  page alike. The assertion was true for a reason that had nothing to do with
  the fix. Only the control sitting beside it — asserting a real page still
  yields its title — went red and exposed it.

## 0.51.0 — 2026-09-02

- **A page can answer 200 and not be the page.** `raise_for_status` was the only
  gate this tool had on "are these bytes the resource we asked for", and it is
  keyed on transport status. A bot challenge served with HTTP 200 sailed
  through it, its bytes reached the comparison, and the disagreement with the
  real article was written down as `unstable` — a claim that the SOURCE is too
  volatile to characterise. What actually happened is that one of our two reads
  was not the document at all: a fact about us, recorded in the library as a
  finding about a citation, which is the exact harm this plugin exists to
  prevent.

  `classify_failure` could never have been widened to cover it. It is reachable
  only from `except` arms, and a 200 raises nothing.

- **The wall is INTERMITTENT, and the earlier note saying otherwise was wrong.**
  A memory file written 2026-09-02 called `pmc.ncbi.nlm.nih.gov` "101/101 a
  reCAPTCHA wall". Measured properly the next day — six reads of one article —
  PMC served the real document 5 times and the challenge once. The earlier
  conclusion came from two probes that both happened to land on the challenge,
  which is a sample, not a property of the host. Intermittence is also what
  makes the defect bite: a verify pass takes two reads, and either one landing
  on the wall produces the disagreement.

  Coverage tells the two apart and was in the output all along: genuine
  volatility on this corpus measures 75–99%, while the article-versus-challenge
  pair measured **0.0000** (360,179 bytes over 3,222 lines against 21,382 over
  33). Near-zero coverage is not a volatile document. It is two categorically
  different responses.

- **Three wrong verdicts, not one, and the worst was invisible until the
  fixture was mutated.** Disabling the gate shows what the library gets
  instead: `ok` — the wall recorded as the cited document's own baseline hash;
  `unstable` — the source called too volatile to characterise; and `changed` —
  an affirmative claim that the cited source has DRIFTED. The last is the worst
  and was the one nobody was looking for. A challenge served identically twice
  makes the two reads agree with each other and differ from the stored hash,
  which is exactly the corroboration `changed` demands, so the safeguard that
  requires a second read cannot help here: both reads are of the wrong
  document. It surfaced only because the first fixture carried no nonce and
  mutation testing asked what it was really proving.

- **The evidence is the document's own words, never a sniff of its text.** A
  reCAPTCHA interstitial sets `<base href="https://www.google.com/recaptcha/
  challengepage/">` — the document declaring, through the mechanism HTML
  provides for exactly that purpose, that it is a challenge page and not the
  article. Deciding from a title string or a phrase in the body would be a
  blacklist, and this repository has already shipped one of those and spent a
  release removing it.

  The false-positive rate was **measured before the rule was written**: 40 pages
  across 40 distinct hosts, drawn at random from rows that had hashed
  successfully. Three carried a `<base>` at all, exactly one was cross-origin,
  and that one was a challenge page — served for a GEO accession the index
  still recorded as a good hash. 0 false positives in 40 is a bound, not a proof
  of zero, and the bound is the honest claim.

- **The gate sits at the read boundary, so no caller can forget it.**
  `hash_page` raises `NotTheResource` before any digest is returned, which
  routes it through `classify_failure` — already the single place a failure is
  given its name. A flag on `PageRead` for each caller to check would have been
  one rule kept in three places. It matters that this covers the FIRST pass too,
  not just re-reads: five `login.tailscale.com` rows are baselined at ~28,000
  stable bytes, a login wall already recorded as a cited document's provenance
  baseline.

- **`blocked`, reused rather than given a fifth word.** That outcome already
  reads "refused; the page may be perfectly fine", which is precisely what a
  challenge is. The address recorded is the challenge's own, not the publisher
  we asked — the 0.38.0 lesson one layer along, where a refusal that names the
  wrong host blames a server that did nothing.

- **Three open-coded copies of one rule became one.** All three fetch sites
  repeated `isinstance(e, (httpx.HTTPError, httpx.InvalidURL))`, so adding a
  fourth kind of failure to two of the three was the defect class that has
  shipped five times here against zero caused by a missing check. They now share
  `FETCH_FAULTS`, and the guard DERIVES its obligation by walking the AST for
  every `try` that fetches — it cannot pass by naming only the call sites alive
  the day it was written, which is the failure the 0.40.0 User-Agent guard had.

- **KNOWN LIMIT, deliberately not fixed.** A SAME-ORIGIN login wall stays
  undetectable and should. For an unauthenticated requester the login page
  genuinely IS what `login.tailscale.com/admin/dns` serves, the same way
  GitHub's 404 on a private repository is a fact about the requester's view
  rather than about the resource. Guessing from a URL path would manufacture the
  finding.

## 0.50.1 — 2026-09-02

- **`--only-host` and `--only-outcome` were silently ignored on `--verify`.**
  Both were parsed, documented, and passed to `snapshot(...)` only, so
  `--verify --only-host github.com` accepted the filter, dropped it, and re-read
  all 3,813 rows while the report called the run scoped. The `--sleep` defect
  again: the CLI complete at one end, the engine complete at the other, and the
  whole fault living in the gap between them where neither review nor a reader
  of either file would see it.

  Found while trying to prove 0.50.0 on the GitHub rows it was built for — which
  is the only reason it was found at all. `verify` now takes both filters and
  narrows on `verify_outcome`, so `--verify --only-outcome unstable` re-reads
  exactly the rows a previous pass could not characterise.

- **The guard that should have caught it could not, and its replacement was
  vacuous until mutation testing said so.** The existing obligation derives from
  argparse and asks whether each flag is read *anywhere* — and `args.only_host`
  IS read, on the branch that did not run. "Used somewhere" is a weaker property
  than "honoured on the path you selected", and only the second is what a user
  means by a flag.

  The replacement asserts the second. Its first version passed on the broken
  code, because it counted every string constant as evidence a flag was handled
  — including `add_argument("--only-host", ...)`, the flag's own declaration. A
  flag existing is not evidence that anything honours it. Caught only by
  reverting the fix and watching the guard stay green.

- **Flags with no meaning for a re-read are refused, not ignored.**
  `--verify --dry-run` and `--verify --retry-failed` now exit with an error
  naming the flag. Accepting them would repeat the same silence in the other
  direction, and loud absence over quiet corruption is the trade this plugin
  makes everywhere else.

- **The host-boundary filter is one function now, not two.** `rows_with_hash`
  needed the same matcher `rows_needing_hash` had, and copying twenty lines of
  LIKE construction would have been a second holder of one rule — the defect
  this codebase has shipped six times against zero caused by a missing check.
  `_host_boundary` is shared, still anchored at both ends, still escaping LIKE
  metacharacters so a scoped re-run cannot silently widen into a full one.

## 0.50.0 — 2026-09-02

- **A nonce no longer reads as provenance drift.** 1,807 of 3,813 verified rows
  — 47% of the corpus — ended as `unstable`, meaning the page did not read the
  same way twice. Measured by fetching pages twice seconds apart and diffing
  them, that verdict was almost never about the source:

      github.com/snap-stanford/Biomni   identical byte length (399,114),
                                        ONE differing line of 1,365:
                                        <meta name="request-id">

  A pull request differed in 100 lines of 2,354, and every one was classified
  individually rather than assumed: 18 signed channel tokens, 14 CSRF fields, 8
  nonces, 8 turbo tokens, 2 request-ids — and the 38 that keyword-matching
  missed turned out to be an A/B bucket (`ui-target: full` vs `canary-1`) and
  per-render UUIDs wiring each button to its tooltip. Zero content.

  Nor is it a GitHub property: doi.org, nature.com, ncbi, huggingface and
  springer all differ by under 1.5% of lines, all telemetry. The digest was
  covering the transport envelope rather than the document, so `unstable` was a
  finding about OUR METHOD reported as a finding about theirs.

- **The volatile bytes are derived, never named.** A list of field names would
  work on today's sample and rot in silence — it cannot fail on a token nobody
  has invented yet, which is the same defect in a guard that the guard exists to
  prevent in the code, and this repository has shipped it twice (`URL_RE`'s
  character blacklist, and a User-Agent guard that named its call sites and so
  could not see a fourth copy appear). Instead `verify` already reads every
  candidate twice; whatever the pair disagrees on is per-request by
  construction. `stable_digest` hashes the rest. A test holds the design: no
  module may contain the string `csrf`, `request-id`, `nonce` or their kin.

- **It declines rather than guessing.** `MIN_STABLE_COVERAGE = 0.90`, because a
  single-line minified document aligns to nothing — 1 of 6 pages sampled, and
  the row simply stays `unstable`. Live coverage on the pages that do align:
  99.51%, 99.58%, 97.91%, 93.68%. The floor's asymmetry is the point: too high
  only leaves rows where they already were, while too low would report a digest
  over a fragment as document agreement — the false reassurance `prefix_agreed`
  exists to refuse one layer along.

- **Three new outcomes, not a widened old one.** `stable_baseline` (first look,
  recorded, nothing compared), `stable_unchanged` (the per-request bytes moved,
  the document did not) and `stable_changed` (the document itself differs).
  Folding these into `unchanged` would put a claim about the whole response and
  a claim about the document inside it under one word, which is how
  `unreachable` came to mean gone AND blocked.

- **The digest is written once and never rewritten**, the same rule
  `content_hash` follows and for the same reason: a pass that found a change and
  then saved today's version over the baseline would erase the finding at the
  instant it made it. The rule lives in the UPDATE's WHERE clause rather than in
  the caller, so two passes racing on one row cannot both see `''`.

- **Two tests were blind and mutation testing caught them.** Removing the
  write-once guard changed nothing any test could see, because `verify` reaches
  it only on a first look — an unreachable guard, the class this project already
  deleted two of in 0.22.0; it is now driven directly. And the chunk-boundary
  test compared split-fed against whole-fed, which both drop an unterminated
  final line and still agree — a proxy assertion where the property was
  correctness. Five mutations are now detected where three were.

- **Memory discipline held.** `hash_page` streams so that page size cannot
  become memory, and a stable digest that needed the body would have quietly
  undone it. Only per-line digests and lengths survive a chunk, bounded by
  `MAX_LINE_DIGESTS`; overflowing records nothing rather than a prefix, because
  a partial list aligned against a full one reports the whole tail as volatile.

## 0.49.0 — 2026-09-02

- **"Installed" now means the root the plugin manager pins, not the marketplace
  clone that root was built from.** The shell trampoline asked
  `installed_plugins.json` which root to run; `staleness.installed_version()`
  asked the marketplace clone's `plugin.json` what version was installed. Two
  files holding one fact, written by different steps of an install and kept in
  step by nothing — on 2026-09-01 their mtimes were eleven hours apart, and in
  that window four captures were refused by the very root the registry had
  pinned. A guard that contradicts the mechanism it exists to back up is not
  defence in depth, it is a second opinion.

  The deleted constant's own comment had the model backwards: *"the cache entry
  the hook executes is built from this clone, so it is the authority on what
  version is installed."* Being the source something was BUILT from does not
  make you the authority on what is DEPLOYED. A clone ahead of the install is an
  update nobody has applied yet, and refusing for it takes capture down for
  every live session in exchange for nothing — the 29-hour outage of 2026-08-24
  reached by a different road.

- **Sixth appearance of this codebase's signature defect, against zero missing
  guards.** Every one has been two holders of one fact rather than an absent
  check: the exclusion rules, the User-Agent, the outcome host, and now the
  installed version. The fix is always deletion, never a reconciler.

- **The suite could not have caught it, and a fixture is why.**
  `conftest._installed_version_matches` stubs `installed_version` out for
  `capture` and `cli` so unit tests do not depend on whatever the developer
  happens to have installed. That is correct, and it meant the COMPARISON was
  covered from the first release while the RESOLUTION — which file is read —
  had no test at all. The same shape as the frozen `now="NOW"` clock and the
  `init_db` that every fixture called: the fixture that makes the suite runnable
  is the thing that makes one bug invisible. `tests/test_installed_version_source.py`
  addresses `staleness` directly, which the autouse fixture does not touch.

- **The registry path was collapsed too, before it drifted.** `staleness`, the
  capture record and the health check each built
  `~/.claude/plugins/installed_plugins.json` from scratch — three holders of one
  fact, one level below the version and not yet disagreeing. It now lives once
  in `registry.py`, and `resolve_pinned` falls back to it. The fallback is
  resolved inside the function rather than as a default argument, because a
  default binds at def time and a test patching the constant would have silently
  read the developer's real registry.

- **A dead parameter went with it.** `installed_version(manifest=...)` was passed
  by nothing anywhere: both production call sites call it bare and no test used
  it, while its docstring claimed "two callers need that" — the end-to-end tests
  use the environment variable, which stays. The `--sleep` defect's shape, a way
  in that looks supported and is exercised by no one.

- **Both guards derive their subjects, and both were seen to fail first.** They
  walk every module under `scripts/` rather than a list of names, because a
  guard that names its subjects cannot fail on a module that does not exist yet
  — which is how a fourth copy of the User-Agent appeared six days after a guard
  was added to prevent exactly that. The marketplace guard failed pre-fix naming
  `staleness.py:56`; the registry guard was mutation-confirmed by reintroducing
  a second holder in `cli.py` and watching it go red. Docstrings are skipped, so
  `registry.py` discussing "two marketplaces" in prose is not a false positive,
  and a vacuity assertion carries the other direction: "nobody outside
  registry.py names it" is also satisfied by a codebase that names it nowhere.

## 0.48.0 — 2026-09-02

- **A 429 now widens that host's interval instead of being recorded and
  ignored.** `RATE_LIMITED = "rate_limited"  # 429 -- back off, conclude
  nothing` said it in the constant's own comment, and nothing backed off.
  `_HostPacer` enforced a fixed per-host interval; a 429 was classified,
  counted, and the next row of the SAME host went out at the same interval.
  Found by running the sweep: a verify pass over the live corpus took 13
  consecutive 429s from github.com in one burst, and resuming would have walked
  straight back into it.

  A fixed interval is our GUESS about what a host tolerates. A 429 is that host
  saying the guess is wrong, and it was the one input the pacer never took —
  the `--sleep` defect's sibling, where the CLI parses a flag and the pacer
  honours it and neither end listens to the answer.

- **Fetching is now one function, so the feedback loop cannot exist in one pass
  and not the other.** There were three `pacer.wait()` + `hasher()` pairs across
  `snapshot` and `verify`; putting backoff in each failure arm would have made
  one rule into three copies, the defect class that has now shipped five times
  here. `_paced_fetch()` waits, reads, and lets the answer change the interval,
  and a guard derives that `hasher(...)` is called nowhere else — which also
  makes it impossible for a future pass to skip pacing the way `verify` did.

  `Retry-After` wins when the host states one; otherwise the penalty doubles
  from a floor of the base interval, capped at 120s, and decays by half on each
  normal answer so one 429 does not tax every remaining row for hours. Only 429
  and 503 slow us: a 404 is an answer, not a complaint, and this corpus holds
  1,285 of them.

- **The guards assert both directions.** A throttled host must slow down AND an
  unthrottled host must not — a "fix" that simply slowed everything would pass
  the first assertion while turning a 2-hour sweep into a week. The penalty is
  per-host, so github refusing us says nothing about arxiv.

- **A derivation moved, and the positive control is what caught it.**
  `_functions_that_fetch()` looked for `hasher(...)` calls; once fetching
  collapsed into `_paced_fetch`, it returned the primitive and nothing else.
  `test_the_derivation_finds_more_than_one_fetching_pass` failed loudly instead
  of the guard silently narrowing to a set that still passed — the failure mode
  that made the User-Agent guard vacuous for six days.

## 0.47.0 — 2026-09-01

- **A refusal now records what CLASS it was, so the health line stops rendering
  three different incidents as three copies of one truncated sentence.** The
  SessionStart check reported: `4 refusal(s) recorded in the last 24 hours
  (refusing to capture: this session is running plugin version , refusing to
  capture: this session is running plugin version , refusing to capture: this
  session is running plugin version ; newest ...)` — three identical reasons,
  each naming no version at all. The records were not identical. They were
  0.42.0-vs-0.41.0, 0.44.0-vs-0.45.0 and 0.45.0-vs-0.46.0.

  `refusal_kinds` is a set, so it deduplicated on the full 409-character message
  and correctly kept three entries; the renderer then cut each at `k[:60]`, and
  `"refusing to capture: this session is running plugin version "` is exactly 60
  characters. The cut landed on the only bytes that differed. `stale_reason`
  states in its own docstring why those bytes exist: *"'your plugin is stale' is
  not actionable, while 'running 0.3.0, 0.11.7 is installed' says exactly what
  happened and implies the fix."* The summary deleted precisely the property the
  message was written to have.

  The width was not the defect, so the fix does not widen it. A set named
  `kinds` was being fed a REASON: a kind is a short closed label — the caps
  `MAX_DISTINCT_KINDS = 16` and `MAX_KINDS_SHOWN = 3` only make sense for one,
  and the hook writer already emits exactly that in `event`. The capture path
  recorded unbounded prose and no label, so `record.get("event") or
  record.get("refused")` fell through to the sentence. Two writers, one fact,
  two shapes: the FIFTH appearance of that class in this repository, against
  zero missing guards.

  So `capture.py` gained `STALE_ROOT_REFUSED` / `INDEX_IDENTITY_MISMATCH` and a
  single `_refuse()` that sets kind and reason together, the kind travels to the
  log beside the prose, and the summary groups by kind. The truncation is
  DELETED rather than tuned — a kind is short by construction, so there is
  nothing left to cut, and prose can no longer reach a fixed-width slot. The
  full reason still goes to `capture.log` complete, which is where a diagnosis
  actually reads it.

- **The guard derives its obligation from the assignment sites, not from a list
  of them.** An AST pass finds every assignment to `result.refused` and requires
  each to be inside `_refuse`, so a third refusal added later cannot ship
  without a class. A guard naming `capture.py:257` and `capture.py:269` could
  not fail on a site that does not exist yet — the same defect in a guard that
  the guard exists to prevent in the code, which is how the User-Agent guard and
  the `--sleep` call-site guard each missed their own subject.

- **Why the suite did not catch it.** Every refusal fixture in `test_health.py`
  builds `_event(ts, "stale-root-refused")` — the hook shape, which has a kind
  and works. Not one test fed the shape the capture path actually writes. The
  fixtures only ever produced the record that already passed, the same way the
  frozen `now="NOW"` hid a batch timestamp and `init_db` in every fixture hid a
  migration the live index had never had.

- Refusals already in `capture.log` have no kind and now read `unlabelled`. The
  label is unrecoverable without parsing the prose, which is the mechanism being
  removed; dropping the records instead would turn a noisy alert into a silent
  one. It self-heals inside the 24-hour window.

## 0.46.0 — 2026-09-01

- **A verify pass now remembers which rows it has read, so a sweep can be run in
  pieces.** `rows_with_hash` took a `limit` and there is no OFFSET anywhere in
  the codebase, and verify recorded nothing — so `--verify --limit 500` returned
  THE SAME first 500 rows on every invocation. The only honest ways to run the
  3,813-row pass were one all-or-nothing job (measured: ~5,760 fetches and ~2.7h
  at `--sleep 2`, because a mismatch spends a second corroborating request) or a
  permanently head-biased sample.

  The fix is not an OFFSET. This exact defect was diagnosed and fixed in the
  OTHER fetching pass, and the migration that fixed it says so in as many words:
  *"Absence of a hash used to mean both 'never attempted' and 'attempted and
  failed', so every pass re-fetched the same dead rows forever and `--limit`
  never got past them."* `rows_needing_hash` therefore selects on recorded
  per-row state and advances; `rows_with_hash` selected on position and could
  not. Same shape as `_HostPacer`: a property of FETCHING that was built into
  `snapshot` and had to be retrofitted to `verify`, because there was nothing
  to inherit.

  An OFFSET would also have been positionally unsound. The order was
  `hashed_at, url_canonical`, ~1,353 live rows share one identical `hashed_at`
  (the 0.34.0 batch-constant bug), and a concurrent snapshot pass rewrites that
  column under the reader — so chunk N+1 would silently skip rows, and a sweep
  that skips rows reports a coverage number it did not earn.

- `verified_at` / `verify_outcome` on `url_index`. `''` means never verified,
  which is true of all 5,028 existing rows. Kept ON the row rather than in a
  cursor table: this project has shipped four stale-second-copy defects and zero
  missing guards, and per-row state stored anywhere but the row would be the
  fifth.

- **Rows are read stalest-first**, which does both jobs with one ordering and no
  new flag. Within a sweep each chunk's rows sort to the back as they are
  stamped, so `--limit N` run repeatedly advances on its own; once the corpus is
  swept the same query restarts it at the least-recently-verified row, because a
  provenance check is not a one-shot.

- **A page we could not re-read is still stamped.** A paywall answered us — that
  is an observation, and leaving it unmarked is exactly what made 1,285
  unreadable rows re-fetch on every pass before 0.36.0. Our OWN faults are still
  stamped with nothing: an internal error advances no cursor and claims no
  verdict, because a row skipped by our bug was never actually verified.

- `verify` still never rewrites `content_hash` or `hashed_at`. Recording that we
  LOOKED is not recording what we FOUND, and the docstring's "read-only against
  the index" claim was corrected rather than left standing — an overclaiming
  docstring is why nobody re-checked the repair tool for a whole release.

- `clock` is required on `verify` and read once per ROW. A parameter that can be
  omitted is the other defect this repo has shipped (`--sleep`, parsed for a
  full release and passed to nothing); a clock read once per RUN is the one that
  stamped 1,348 rows with their batch's start time.

- The verify report ends with `never verified: N (rows remaining)`, so a chunked
  sweep can tell whether it is finished.

- 1,050 tests. New `tests/test_verify_cursor.py` reuses the existing
  behaviour-derived list of fetching passes rather than writing a second copy of
  it, and asserts every such pass advances under a limit — the same test passes
  for `snapshot` and failed for `verify`, which is the control that makes the
  result mean something. Verified by real execution against a copy of the live
  5,028-row index: the migration applied, two chunks read six distinct URLs with
  six distinct timestamps, and production was confirmed byte-identical
  afterwards.

## 0.45.0 — 2026-09-01

- **A hash now states what it covers, so the biggest sources stop being blank.**
  71 rows were fetched SUCCESSFULLY and held no evidence at all — no hash, no
  size, nothing any later pass could compare — and they are the corpus's largest
  citations: 39 arXiv PDFs, a Nature paper, an SEC filing, several genome
  assemblies. The cause was not memory (`hash_page` already streamed) and not the
  cap. It was that `content_hash` was a single column with no room to state its
  scope, so `''` meant BOTH "never read" and "read fine, refused to record" —
  the same one-word-for-two-findings defect that `unreachable` (gone vs blocked)
  and `gone` (absent vs not-visible) each cost a release. Given that column the
  all-or-nothing rule was FORCED, not chosen: a prefix stored where a
  whole-document hash is expected really would compare equal for two documents
  differing after the cap.
  Sizes were measured before anything was written, because the shape of the
  number decides the fix: median **11.8 MiB**, largest **207 GiB**. So "raise the
  cap" could never have been the answer — it moves the tripwire and leaves the
  same silence for the datasets. HTTP (`Content-Range`, `Repr-Digest`), git blobs
  and BitTorrent pieces all answer this identically: a digest is always scoped
  and the scope travels WITH it. `PageRead` now carries `covers_bytes` and
  `complete`, neither defaulted; `set_content_hash` requires both; `TooLarge`,
  the `too_large` outcome and its branch are DELETED. A capped read is not an
  exception, it is a read of a stated prefix.
- **A prefix hash is one-directional, and `verify` is now unable to forget it.**
  A difference inside the covered range proves the document changed; agreement
  proves nothing whatever about the bytes past the cap. "The first 5 MiB of a
  39 MiB PDF are unchanged" reported as "unchanged" would manufacture a
  reassurance — strictly worse than the silence it replaced, because nothing
  downstream could tell it was hollow. Those land in `prefix agreed`, never in
  `unchanged`. Each row is also re-read over EXACTLY the span its stored digest
  covers, or every truncated row would report a change the moment the cap moved:
  a finding about our own configuration wearing the costume of a finding about
  the source.
- **The cap is now a cost bound, and is set where completeness actually lands.**
  `HASH_MAX_BYTES` 5 MiB → **32 MiB**, which covers three quarters of the
  oversized rows outright, plus a wired-and-guarded `--max-bytes`. It does not
  chase the tail and nothing should.
- **Our own bug was being written into the library as link rot.** Found by this
  release rather than looked for: changing the hasher's signature made the old
  stubs raise `TypeError`, `except Exception` caught it, and 20 rows were stamped
  `unreachable`. `classify_failure` was right to send anything unrecognised to
  UNREACHABLE rather than GONE — but that is a rule for unrecognised NETWORK
  errors, and a `TypeError` is not a fact about somebody's citation at any
  confidence. Both fetching passes now record nothing at all for a non-HTTP
  error, count it, and log it with its traceback; the run continues, because a
  pass over this corpus takes hours and one strange row must not discard the rest.
- Migration defaults describe what actually happened rather than what is
  convenient: every row hashed under the old rule IS complete, so
  `hash_truncated = 0` is a fact about those ~3,744 rows — while their length was
  never recorded, and `hash_bytes = -1` says exactly that. A default of 0 would
  have claimed a zero-byte document and any other number would have invented one.

## 0.44.0 — 2026-09-01

- **A verify pass now corroborates a change before reporting one.** The first
  real run of `--verify` called **29 of 60** pages CHANGED in five days. A
  control — read the same page twice, three seconds apart — found **5 of 8
  differ from themselves**, because a whole-document hash of live HTML also
  covers nonces, ad tokens, build ids and timestamps. A single mismatch is a
  CANDIDATE, not a finding: reporting it as drift is an affirmative claim about
  the source drawn from one observation that cannot support it, the same error
  as reading a 404 as absence. A candidate is now read again; only a page that
  agrees with ITSELF and differs from the stored hash is CHANGED, and one that
  does not is reported `unstable`. Re-run of the same 60 rows: **CHANGED 29 →
  12, unstable 17, unchanged 30 either way.** The second read is spent only on
  candidates, so an unchanged page still costs one request.
- **`--sleep` reached `snapshot` and not `verify`.** The flag parsed, the help
  text promised politeness, and the verify branch called `verify(db_path,
  hasher=..., limit=...)` — which had no pacing parameter at all. A full pass is
  3,744 pages, including hosts already rate-limiting us, fetched back to back.
  This is the FOURTH appearance of this defect class here, and the existing guard
  did not fire because it derived its flags by matching the literal prefix
  `--only-` and its consumers by matching the literal name `snapshot`: two
  hand-written lists wearing the costume of a derivation. The consumer set is now
  derived from BEHAVIOUR — a function that calls `hasher(...)` makes outbound
  requests, so it must accept pacing and every call site must pass it.
- Per-host spacing is now `_HostPacer`, one definition shared by both fetching
  passes. It lived inline in `snapshot`, which is exactly why `verify` had none:
  there was nothing to reuse, so politeness was a property of one loop rather
  than a property of fetching.
- A verify failure now records WHY (`blocked`, `gone`, `timeout`...) instead of
  collapsing everything into "unreachable" — the conflation 0.36.0 removed from
  the other fetching pass, still present in this one.

## 0.43.0 — 2026-09-01

- **A GitHub 404 can now be settled with your own credentials, and only in one
  direction.** GitHub 404s a private repository on purpose, so anonymously
  "deleted" and "not yours to see" are identical. 0.39.0 discriminated the rows
  whose immediate parent is also hidden, but a repo ROOT's parent is a public
  profile page — leaving 15 rows asserting the owner's own private repositories
  were dead links, `musharna/zotero-provenance` among them. New
  `scripts/corroborate_github.py` asks `/repos/{owner}/{repo}` for EXISTENCE
  ONLY: it never fetches, hashes or stores repository content, and the only
  thing it changes is a label on a URL the library already holds. It can only
  DOWNGRADE `gone` to `not_visible` — a 404 with a token still means *this
  credential cannot see it either*, and promoting that to deletion would rebuild
  the original defect with more confidence behind it. There is deliberately no
  `GONE` in that module's vocabulary. Capture and snapshot stay credential-free;
  a token is read at call time, never stored, and its absence changes nothing.
- **An install specifier is not a dead link.** `https://github.com/o/r.git@<sha>`
  is a `pip install git+...` line: it names a live repository at a real commit
  and 404s because the PATH is not a page. Three rows reported link rot about
  repositories that are fine. Such a row now records `malformed` — we never
  asked a well-formed question. Deliberately NOT repaired into the repo root:
  stripping `.git@<ref>` yields an address that resolves but was never cited,
  and the commit pin is the point of a requirement line. Same error as appending
  a closing paren, with a more convincing result.
- **The hook's diagnostic trail is now a decision instead of a default.** 0.37.0
  found that WARNING reached `capture.log` only through `logging.lastResort`, and
  could not add the textbook `NullHandler` without silently deleting that trail.
  `configure_hook_logging()` reproduces exactly the bare `%(message)s` shape
  lastResort was already writing — so existing log lines do not change — and the
  package now carries a NullHandler like any other library. The SessionStart
  health check deliberately does not opt in: its stderr feeds a file whose being
  empty is the signal.
- Lint clean: nine unused imports removed, plus a duplicate dict key in
  `test_health.py` (both values were `0`, so no expectation changes).

## 0.42.0 — 2026-09-01

- **A URL move now writes both stores, or neither.** A captured URL is held
  twice: `item.url` in the library and `url_canonical` in the index, where it is
  the PRIMARY KEY. They are one identity with two copies. On 2026-08-22 a one-off
  script in a scratch directory corrected 24 truncated URLs with
  `z.update_url(key, new)` and never wrote the index; nine days later `snapshot`
  fetched the stale index strings, got 404s, and recorded `gone` — a repair
  manufactured the link rot 0.41.0 was then built to undo. `repair.py` had always
  done both writes, but that was a CONVENTION in one function's body, and a
  convention cannot bind a script written at 2am against the client directly. The
  pairing is now `url_move.move_url`, whose signature cannot be satisfied without
  a db_path, and the client's one-store URL write is private. The fix is the
  absence of a public one-store verb, not the underscore.
- **The trashed-item rule is defined once instead of in one writer of four.**
  `_try_add_tags` refused a trashed item and said why — "tagging a trashed item
  would quietly resurrect provenance onto something the user removed" — while
  `record_content_hash` and the URL write never checked, so `snapshot` would
  stamp provenance onto an item the user had thrown away (reachable: 7 live index
  rows pointed at trashed items). Zotero's trash is a FLAG: a trashed item reads
  **200 OK with `deleted: 1`**, not 404, which is exactly why a status-code check
  could not see it. Every writer now goes through one `_open_for_write` gate that
  makes all three checks — exists, not trashed, still the item selected — and the
  guard DERIVES the set of writers from the source rather than naming them.
- **`scripts/verify_index.py` compares the two stores in BOTH directions.** Every
  other maintenance tool starts from `SELECT ... FROM url_index`, so none of them
  can see an item the index does not name. Paging the collection the other way
  found 49 stranded items — **30 of which had already caused a duplicate**, since
  an item nothing indexes is invisible to `lookup_url` and the next citation of
  that URL creates a second one. Read-only, and it separates a real fault from
  the recoverable-claim protocol working as designed.

## 0.41.0 — 2026-08-31

A 404 to an address we mangled is a fact about our record, not about the source.

- **34 rows in the live index hold a PREFIX of the address that was cited, and
  `snapshot` was stamping them `gone`.** Until 2026-08-21 the tokenizer was a
  character blacklist — `[^\s<>"\'`\)\]]+` — and `)` and `]` are legal URL
  characters, so every cited address containing one was stored cut off at it.
  Commit `1d46bd5` fixed the tokenizer; nothing repaired what it had already
  written. Reproduced byte for byte against that commit's parent — the stored
  rows come back character-identical:

      cited  https://en.wikipedia.org/wiki/Aestivation_(botany)
      stored https://en.wikipedia.org/wiki/Aestivation_(botany     ← the live row

  Measured with a control: **0 of the 269 rows captured since the boundary work
  are truncated; 33 of 33 captured before it are.**

- **`unbalanced_brackets` now gates `gone`.** This is `absence_is_corroborated`
  one step further back: that asks whether we may read a 404 as absence, this
  asks whether we ever sent the address that was cited. A row whose brackets do
  not balance is recorded `malformed` — a statement about our record — and no
  request is spent probing its container. The suspicion is deliberately not
  treated as proof: `(` is a legal sub-delimiter, so a real address can trip it.
  That error costs one downgraded claim; the opposite prints link rot that never
  happened.

- **`repair` never invents the missing closer, and its docstring no longer
  implies it looked.** It said "re-balance a paren" while the code only ever
  removed an EXCESS closer — which is why the whole class survived every cleanup
  pass, reporting nothing to recover. Measured: 0 of 34. Guessing is genuinely
  wrong, not merely unproven — for `File:...Moneymaker_tomato_plant_(Solanum_lycopersicum`
  the appended `)` **404s** while the address recovered from the transcript
  returns **200**.

- **The truncation merged distinct citations, not just shortened them.**
  `S0092-8674(26)00697-5` and `S0092-8674(26)00174-1` are two different Cell
  papers; both truncated to `.../S0092-8674(26`, which is the index primary key.
  Two cited sources became one row and one Zotero item. No URL repair recovers
  the second — it is flagged and left alone rather than guessed at.


## 0.40.0 — 2026-08-31

Identity belongs to the client, not to whoever remembers to send it.

- **53 rows recorded `blocked` against Wikimedia for a page it serves to
  anyone who asks politely.** `snapshot.py` sent `User-Agent: zotero-provenance`
  — no version, no contact URL — and Wikimedia's policy rejects that. Measured
  live 2026-08-31, same address, seconds apart: the bare string gets **403**,
  the project's shared string gets **200**. The library was asserting a refusal
  that was our own doing and reading it back as a fact about the source.

- **The cause is a fourth copy of the User-Agent, written six days after the
  release that consolidated it.** `e619c31` (0.6.0, 2026-08-21) made the package
  the single source and added `tests/test_version.py` to hold it there.
  `snapshot.py` arrived on 2026-08-27 declaring its own
  `_USER_AGENT = "zotero-provenance"`. Its own docstring warned that a header
  two call sites must remember is "one rule kept in two places, which is how the
  exclusion rules drifted apart" — and then kept it in two places.

- **The guard could not have caught it, because it named its subjects.** One
  test per module — `test_zotero_client_…`, `test_title_fetcher_…`,
  `test_setup_script_…` — so the set it checked was the set of callers alive on
  the day it was written. A hand-maintained list of call sites cannot fail on a
  call site that is not on it. That is the same defect, expressed in a test,
  that the test exists to prevent in the code. It is this project's **fourth**
  stale-second-copy, against zero missing guards.

- **The User-Agent is now a default on the client, set once in
  `build_fetch_client`.** No call site passes the header, so no call site can
  get it wrong; httpx merges it into every request. Both module-level
  `_USER_AGENT` constants are deleted — there is no longer a value to drift.

- **Four guards replace the one, and every one of them was seen to fail
  first.** Against the pre-fix source all four go red, and the behavioural one
  names both copies in its message: a literal-detector that also walks
  *assignments* (the first version passed on the broken code, because the drift
  hid one hop away behind a constant); an AST sweep asserting every `httpx.Client`
  in the package sets a UA at construction; an assertion that the built client
  carries the shared string; and a sentinel test that hands every fetch path a
  client with a known identity and fails if any path overrides it.

- **The UA test now drives `build_fetch_client` instead of its own
  `httpx.Client`.** It hand-rolled a client production never builds, so when the
  header moved it was the *test's* client that lost its identity. `transport=`
  was added to the builder for this: a boundary test has to drive the thing that
  ships, and it is still wrapped in `GuardedTransport`, so the address guard is
  exercised rather than bypassed.

- **`--only-host` narrows a re-run to one host and its subdomains**, so a fix
  affecting one host family can be proved on it before a wide re-run. It matches
  on a boundary, never a substring: `wikipedia.org` takes `en.wikipedia.org` and
  refuses `notwikipedia.org`, `wikipedia.org.evil.test`, and the name inside a
  path. LIKE metacharacters are escaped — an unescaped `%` would widen a scoped
  re-run to the whole corpus while the report still called it scoped. Most of
  its tests are about what must *not* match; this project has shipped a
  substring where it meant a token three times.

- **The call-site guard now derives its obligations from argparse.** Its first
  version named `only_outcome` and would have passed unchanged the day
  `--only-host` was threaded into one of the two `snapshot()` calls — the
  `--sleep` defect for the third time. Every `--only-*` flag the parser defines
  must now reach every call site, so adding a flag adds the obligation
  automatically. Mutation-checked: removing `only_host` from one call site turns
  it red.

## 0.39.0 — 2026-08-30

A 404 to an anonymous request is not proof the page is gone.

- **219 rows asserted that the owner's own private pull requests no longer
  exist.** They exist. GitHub answers 404 rather than 403 for private
  repositories, deliberately, so it does not leak which ones are there — and
  `classify_failure` took the status at face value and wrote `gone`. Proven
  three ways: unauthenticated 404; authenticated 200 with a real title and
  `private: true`; and a genuinely public repo of the same owner returning 200
  from the same host seconds apart, which rules out a blanket block. Since the
  library syncs, that false claim is the version that propagates.

- **The cause is that `gone` states a fact about the RESOURCE, derived from a
  status code that is only a fact about THIS REQUESTER's view of it.** We fetch
  with no credentials, so 404 confounds "no longer there" with "there, and not
  visible to you". A host-specific exemption for github.com would have left
  GitLab, Bitbucket, private wikis and every other 404-on-private host wrong.

- **The discriminator is containment, and it needs no credentials.** If the
  immediate parent is also invisible, the whole subtree is hidden and absence
  cannot be claimed (`not_visible`). If the parent answers and only the leaf is
  missing, the leaf really is missing (`gone`). Measured on both before it was
  written: private `/owner/repo/pull/3` 404 with parent 404; public
  `/owner/repo/pull/99999` 404 with parent 200. The IMMEDIATE parent, because
  in the private case the grandparent — a user profile — answers 200, so walking
  to the topmost reachable ancestor would have re-made the same false claim.

- **Absence is never the default.** With no prober the outcome degrades to
  `not_visible`, the weaker claim that is always true, because absence is the
  thing we would be inventing. A URL with no parent keeps `gone`: there is
  nothing left to ask, and refusing to say `gone` for a site root would throw
  away the real finding to avoid a rarer one.

- **`--only-outcome` re-runs a pass over just the rows a classification change
  affects.** Correcting 306 misjudged rows would otherwise have meant re-fetching
  all 1,400, asking academic.oup.com for 159 pages it had refused an hour
  earlier purely as collateral. Politeness is a reason for a feature, not only a
  delay.

- **The `--sleep` defect recurred while writing this, and is now guarded.** The
  new flag was threaded into the real `snapshot()` call and not the dry-run one,
  so `--dry-run --only-outcome gone` silently reported the unattempted set
  instead — 76 rows where 306 were meant. Both ends looked complete; only the gap
  was wrong. An AST test now derives every `snapshot()` call site from the source
  and asserts each passes the filter.

## 0.38.1 — 2026-08-30

A 404 is not a refusal, and the new report said it was.

- **The "who refused us" tally merged 403s with 404s, and the backfill exposed
  it immediately.** Run over the live index, the list ranked `github.com` first
  with 243 — of which **241 were `gone`**, dead links that github had served
  perfectly correctly. Naming a host as having "refused us" for serving an
  honest 404 collapses gone into blocked one level up from the collapse 0.36.0
  exists to have fixed, and the two call for opposite remedies: ask for access,
  versus repair or retire the citation. They are now two tallies.

- **`too_large` is charged to neither.** That is US refusing — the host served
  the document and we declined to hash past the cap. Listing it under a host's
  name attributes our own decision to them. Its address is still recorded,
  because where the bytes came from is a fact either way.

- Both mutations are covered: merging `gone` back into the refusal tally fails
  exactly `test_a_dead_link_is_not_counted_as_a_refusal`, and charging
  `too_large` to a host fails exactly
  `test_an_oversized_page_is_charged_to_neither_host_tally`. The separation is
  asserted in both directions, because one assertion alone is satisfied by a
  tally that is simply always empty.

## 0.38.0 — 2026-08-30

An outcome named the URL we asked for, not the one that answered.

- **177 rows recorded `blocked` against doi.org, and doi.org had done nothing
  wrong.** Measured, not inferred: `https://doi.org/10.1093/bioinformatics/bty895`
  answers `302` and redirects to `academic.oup.com`, which answers `403`. The
  index stored the refusal against the resolver, whose name is the only one in
  the row — so the library blamed a party that had behaved correctly every time
  and never named the publishers actually refusing us. With `academic.oup.com`
  also holding 87 rows in its own right, the real scale of a single refusing
  host was split across two names and visible under neither.

- **The cause was the fetch layer's contract, not the loop that used it.**
  `hash_page` returned a bare digest, so the address of the page it had just
  read was discarded at the return boundary and every caller downstream had no
  choice but to assume the requested URL was the answering one. Fixing only the
  failure branch — reading `exc.response.url` where the 403 arrives — would have
  left the successful path still discarding it, and the next consumer would have
  re-made the same assumption. `hash_page` now returns a `PageRead` carrying the
  digest AND the URL that served it, and there is deliberately no way to get a
  bare digest out of the module.

- **`final_url` is a third state, not a second one.** Empty means *we never found
  out*, NOT *there was no redirect*. The ~4,900 rows written before the column
  existed never had their address checked and must not be made to claim they
  did — the same reason an unrecognised fetch error is never allowed to become
  `gone`.

- **`set_fetch_outcome` takes `final_url` as a required argument.** Its natural
  default would be `''`, the value meaning "unknown", so a caller that simply
  forgot it would write a confident absence over a fact it was holding. This
  codebase already shipped one parameter that could be omitted and therefore was
  (`--sleep`, parsed for a release and never passed); requiring it turns that
  failure from runtime silence into an immediate `TypeError`, which is how all
  19 existing call sites announced themselves.

- **The report attributes refusals to the host that answered.** A run now ends
  with "who refused us", keyed after redirects. The same tally keyed by
  requested host put doi.org on top with 177 and named no publisher at all.

- **The tests drive real redirect chains through httpx.** A hand-built
  `HTTPStatusError` would carry whatever URL the test put on it, proving only
  that the assertion matches its own fixture; what is under test is whether
  httpx's redirect handling and our reading of it agree. Each new test was run
  against a mutated source and confirmed to fail for its stated reason — the
  band-aid version (success path keeps the requested URL) is caught by exactly
  one test, which is what makes "this is a mechanism removal" a measurement
  rather than a claim.

## 0.37.0 — 2026-08-30

The maintenance CLIs logged nothing, and their warnings did not look like warnings.

- **`snapshot_pages.py` ran for over an hour against the live index and printed
  nothing at all.** Every `logger.info` in the package sat under a root logger
  that no entry point had ever configured, so INFO fell below the default
  WARNING threshold and was dropped. Measured on the live run rather than
  inferred: 226 rows classified, 0 bytes of log. For the whole run, silence and
  a hang were indistinguishable.

- **WARNING and ERROR were worse than silent — they were unlabelled.** With no
  handler anywhere, Python falls back to `logging.lastResort`, which writes the
  bare message to stderr with no level, no timestamp and no logger name. In a
  run log, `prune failed for <url>` sat among ordinary output with nothing
  marking it as a failure.

- **Fixed for all nine maintenance CLIs at once**, not in the one script that
  exposed it. A rule kept in nine places is the rule the tenth tool omits, which
  is how this codebase has lost a rule before. The new test *discovers* the
  entry points rather than listing them, so a CLI added later that forgets to
  configure logging fails the suite.

- **Two paths deliberately keep the unconfigured behaviour, and the test asserts
  that direction too.** The capture hook appends its stderr to `capture.log`, so
  those bare lastResort lines are its human-readable diagnostic trail — the
  textbook library fix, a `NullHandler` on the package, would have silently
  deleted them. The SessionStart health check sends stderr to
  `health-errors.log`, a file whose being empty is the signal. The guard asserts
  both directions: the nine configure logging, the two do not.


## 0.36.0 — 2026-08-29

"unreachable" was one word covering two findings that mean opposite things.

- **A 404 is a fact about the source; a 403 is a fact about us.** `snapshot`
  recorded both as `unreachable`. One is the provenance finding this tool exists
  to produce — the citation no longer resolves. The other says nothing about the
  page at all: we were turned away at the door and it may be perfectly intact.
  Collapsed together, the library asserts link rot that never happened.

- **Measured on the live corpus before writing anything.** Of 1,285 rows the
  full pass could not read, whole hosts had failed at exactly 100% —
  academic.oup.com 87/87, pubmed 83/83, en.wikipedia.org 36/36,
  sciencedirect 32/32, cell.com 17/17 — while arXiv hashed 195 of 232 and GitHub
  420 of 663. Link rot does not cluster per-host at 100%. Probing returned 403
  for every one of those hosts and genuine 404s for GitHub's failures.

- `last_outcome` and `last_attempt_at` are now stored per row, and
  `classify_failure` is the single definition of what a failure was:
  `gone` (404/410), `blocked` (401/403), `rate_limited` (429), `server_error`
  (5xx), `timeout`, `too_large`, `unreachable`. **Only 404 and 410 may become
  `gone`** — anything unrecognised falls to `unreachable`, because guessing
  "gone" from an error we do not understand would manufacture the exact finding
  the tool exists to report truthfully.

- **A failed attempt is now recorded as an attempt.** Absence of a hash used to
  mean "never tried" and "tried and failed" indistinguishably, so every pass
  re-fetched all 1,285 dead rows and `--limit N` never advanced past the first N.
  Proven on the live index: two consecutive `--limit 5` runs examined the same
  rows this morning; they now examine different ones. `--retry-failed` reaches
  them again. `ok` is deliberately not a failure — a fetch that succeeded but
  whose Zotero stamp was refused still has no hash and must be retried, which an
  EXISTING test caught when a mutant excluded it.

- `hashed_at` still stays empty on a failure. `test_a_page_that_could_not_be_read_
  consumes_no_timestamp` asserted `clock.reads == 0`, a PROXY for that property
  which stopped being true once an attempt was legitimately timestamped. It is
  replaced by the property itself — `hashed_at == ''` and `content_hash == ''` —
  which is stronger: a stored read-time now fails the test even if it came from a
  value the loop never asked the clock for.

### The schema could go stale under any tool that opened it

Found by real execution, and unreachable by the suite. `snapshot_pages --limit 5`
died on the live index with `no such column: last_outcome`.

`init_db` is the only thing that applies MIGRATIONS, and **2 of the 8 maintenance
CLIs called it**. So a column added FOR a maintenance tool was missing in exactly
the tool that needed it — and the same crash was available on `content_hash`
since the release that added it. Every test fixture calls `init_db` first, so no
test could ever have seen this.

Writing the call into the other six would be one rule kept in eight places, and
the ninth tool would omit it; this codebase's three worst defects have all been a
second copy of one rule going stale, against zero caused by a missing guard. So
`_connect` now guarantees the schema, once per process: there is no longer a step
that can be skipped.


## 0.35.0 — 2026-08-29

`--sleep` said "be polite" and did nothing at all.

- **The flag was parsed and never passed.** `snapshot_pages.py` declared
  `--sleep ... help="seconds between pages (be polite)"`, and `snapshot()` had no
  parameter to receive it. Both call sites omitted it. Every page of the first
  1,355-row pass went out back to back at full speed, and the next 3,521 were
  about to do the same.

  Its two siblings honour theirs end to end (`prune.py`, `backfill.py`). This is
  the same family as the `-> None` that returned `False` and the `now: str` that
  carried a batch constant: a declared contract with nothing behind it. The
  tell is that the defect is invisible from either end alone — the CLI looks
  complete, and `snapshot()` looks complete.

- **Swept the whole class rather than the one instance.** An AST pass over every
  `add_argument` in `scripts/` against every attribute read in the same file:
  35 flags across 10 scripts, exactly one dead. Measured, so the count is a
  finding rather than an assumption — the fifth miscount on this codebase's
  surfaces came from doing the opposite.

- **The delay belongs PER HOST, not per run.** `rows_needing_hash` orders by
  `first_seen`, and a session reading one site captures its pages within minutes,
  so same-host rows arrive in contiguous bursts. A per-run delay is the wrong
  shape twice over: it waits between two unrelated hosts, where waiting buys
  nothing, and inside a burst it is the only thing standing between this tool and
  hammering one small site. The remaining corpus spans 963 hosts — the head is
  GitHub, doi.org and arXiv, but the tail includes small wikis with 25 rows each.

- **The interval runs between request STARTS, and a failed fetch counts.** Time
  the host already spent serving us counts toward the wait, so a slow fetch is
  not paid for twice. A dead link and an oversized page are stamped like any
  other request: on an old corpus a long run of failures is the likeliest way to
  end up sprinting through one site, and `TooLarge` is raised only after the body
  has been pulled, making it the most expensive request the host serves.

- Default is 2.0s per host, off (`0.0`) in the library function so `verify` and
  existing callers are unchanged.

- **Verified against the real CLI, with a control.** Five live rows containing one
  same-host pair: 49s at `--sleep 45`, 2s at `--sleep 0`. Without the control the
  49s proves nothing — slow fetches look identical. Both mutants were watched
  failing first: per-run instead of per-host trips only
  `test_different_hosts_are_not_made_to_wait_for_each_other`, which is precisely
  why that test exists; stamping the host on success only trips both
  failed-fetch tests.

- Known and not fixed: an unreachable row is never recorded as attempted, so it
  is re-fetched on every pass and, under `--limit`, permanently blocks the head
  of the queue.


## 0.34.0 — 2026-08-27

`hashed_at` recorded when the RUN started, not when the page was read.

- **Found by looking at the live index during the first real hashing pass**, not
  by review: 1348 rows shared a single `hashed_at`. `snapshot` took `now: str`
  and threaded that one value through every row, so every page in a pass carried
  the timestamp the pass began. A full run over this corpus takes hours; a page
  read at the end was stamped with the hour it started.

  `set_content_hash` documents the field as "what the page said, **and when it
  was read**". The signature promised a per-page fact and the caller handed it a
  batch constant — the same shape as the `-> None` that returned `False` in round
  8. A provenance timestamp that is confidently wrong is worse than a coarse one,
  because nothing downstream can tell.

- **`now: str` is gone, replaced by a `clock` callable read once per page.**
  Removing the parameter removes the mechanism: there is no longer a value that
  *could* be threaded through the loop. Tests keep their determinism by injecting
  a fixed clock, which is what made the bug invisible — every existing test
  passed a CONSTANT, and a batch constant is indistinguishable from a per-page
  clock when the clock never moves.

- **Sampled after the fetch returns, not before**, so a dead link spends no
  timestamp: a `hashed_at` is a record that a page *was* read. Asserted with a
  counting clock, and mutation-tested in both directions.

- Rows hashed before this fix carry the start time of their run rather than their
  own read time — wrong by up to the run's length. The hashes themselves are
  correct; only the reading time is coarse, and re-fetching thousands of pages to
  sharpen a timestamp is not worth the traffic.

## 0.33.0 — 2026-08-27

The exclusion rules lived in two copies, and the cleanup tools held the stale one.

- **A rule capture enforced could be invisible to every tool that cleans up.**
  `url_processing.is_excluded` decides what capture refuses; `retire.classify`
  decided what maintenance could reach, as a second hand-maintained copy of the
  same rule set. Nothing kept them in step.

  0.31.0 is the proof, and it was this project's own release note that was wrong:
  it added the malformed-DOI rule and said prune would sweep the junk rows it
  described. Six rows — `10.1/ABC`, `10.x`, `GSE12345`, a bare `…`, and two
  spellings of `doi.org` with no DOI at all — were still in the live collection
  afterwards. Capture refused them; `retire --policy` reported "0 to retire,
  4826 left alone", because `classify` had never heard of the rule. The note was
  written from what the code should have implied rather than from a run.

  (The count in that note was also wrong: four, not six. Two bare `doi.org` rows
  had no path to notice. This surface has now been miscounted five times running,
  and the lesson has not changed — derive the set from the definition, not from
  the instances already in hand.)

- **Fixed at the mechanism, not the instance.** Adding a `doi.org` branch to
  `classify` would leave the second copy in place and hand the identical gap to
  whichever rule is added next. Instead `classify`'s fallthrough now asks
  `is_excluded` directly, so a rule added to the capture path is reachable by
  maintenance the moment it exists. This is the third time a defect here has been
  a stale second copy of the truth rather than a missing guard.

- **Opt-in, and bounded in both directions.** The fallthrough returns the POLICY
  tier, which requires `--policy`: reaching back to trash a row on the strength
  of a rule written after it was captured is a product decision, not a proof
  about the address. And `test_exclusion_reachability` asserts the property in
  both directions — everything capture refuses must be nameable by maintenance,
  and everything capture accepts must be left alone. A fallthrough that named
  every row would satisfy the first half completely while deleting the library.

## 0.32.0 — 2026-08-27

Wave 6: what a source was cited FOR, kept on this machine.

- **The library could say a source was consulted, never what for.** That is the
  question an audit of your own bibliography actually asks, six months after the
  conversation that produced the citation, and a list of URLs cannot answer it.
  Capture now records the sentence each URL appeared in.

- **Local-only, deliberately.** Three designs were possible — a Zotero child
  note, a short quote in `extra`, or the local index — and the difference between
  them is privacy, not effort. The Zotero library SYNCS, so the first two push
  fragments of conversations to a third party's servers. Local-only is also the
  reversible one: a local row can be promoted to a note later, but a note that
  has already synced cannot be recalled. Claim text is written to `claim_link` in
  the sqlite index and nowhere else — not tagged, not in `extra`, not in the
  capture log, which records only the COUNT.

  The property is asserted by execution rather than by convention: `test_claims`
  runs a real capture and checks every outbound payload for the claim text, with
  a positive control in the same test so it cannot pass by recording nothing.
  Made to fail first by leaking the claim into a tag.

- **The extraction is deliberately dumb.** The sentence around the URL, as
  written — no summarising, no inference, no asking a model what the claim
  "really" was. A stored sentence can be read and judged by a person; a generated
  paraphrase is one more thing that can be wrong about a source, filed under
  provenance. Scanning never enters the URL's own span, or a URL's dots would
  split it into a sentence of its own tail.

- **A URL alone on a line records nothing.** Storing the bare URL back as its own
  justification would answer the question with the question.

- **Bounded, and refusing rather than evicting at the bound.** 400 characters per
  claim (truncation is marked), 50 distinct claims per URL — repeats collapse onto
  one row and count. At the cap new claims are refused, which is the choice the
  retry queue already makes and for the same reason: the earliest claim is the
  provenance, and a later mention is not a better record of it. An existing claim
  still updates at the cap, or the bound would freeze a row's history too.

- **Recorded before any network call**, so a run in which every Zotero write
  fails still leaves a record of what was being cited. A fault in claim recording
  is reported in `errors`, not raised: losing the primary library capture because
  a secondary annotation failed is the worse outcome.

- **`scripts/show_claims.py`** asks the questions: coverage, one URL's claims, or
  a search across all of them. Read-only.

- Claims are captured going forward only. There is no way to recover the sentence
  around a URL cited before this existed — the conversation is not kept — and the
  tool says so rather than reporting an empty result as if it meant something.

## 0.31.0 — 2026-08-27

Wave 7: a captured DOI has to be a DOI, and it has to still stand. Both checks
found live damage.

- **Four doi.org URLs in the library were not DOIs at all.** `10.1/ABC` and
  `10.x` (placeholders typed in prose), `GSE12345` (a GEO accession given a
  doi.org prefix by mistake), and a bare `…` — an ellipsis the extractor lifted
  out of truncated text and filed as a source. `is_excluded` now rejects a
  doi.org URL whose path is not syntactically a DOI, which means `prune` sweeps
  the four already in the collection: the same mechanism the RFC-2606
  reserved-name rule used, where adding the rule cleaned the backlog instead of
  needing a second list of what counts as junk.

  Syntax only. It rejects strings that cannot be a DOI and never asks whether a
  well-formed one resolves — `doi.org` answers that, and a valid DOI that 404s
  is a dead source rather than a malformed one.

- **`scripts/verify_dois.py` asks CrossRef and Retraction Watch.** A provenance
  library records what was consulted; it does not notice on its own that one of
  those sources was retracted six months ago. `ghostcite` does that work and is
  audited, so this shells out to it — a second implementation of a byline gate
  is a second thing to be wrong.

- **It found four truncated DOIs on its first real run**, in a 40-DOI sample:
  `10.1016/0022-2836(70`, `(81`, `s0022-2836(05`, `s0092-8674(00` — all cut at
  an unbalanced opening parenthesis. **Legacy, not ongoing**: the current
  extractor was checked against four parenthesised-DOI spellings and handles
  every one, and all four bad rows predate the 0.11.x boundary move to
  linkify-it-py. The truncated suffix is not derivable from what was stored, so
  `repair` cannot correct them; surfacing them is the point.

  The two layers separate cleanly — the syntax rule leaves these alone because
  they are _plausible_ DOIs, and the resolution gate catches them because they
  do not resolve.

- **A tool that could not run is not a pass.** Zero findings from a missing
  binary would otherwise be byte-identical to a clean corpus, which is how a
  broken check becomes a silent all-clear. `unavailable` is reported, the exit
  code is 2, and a DOI ghostcite could not resolve is counted as _unknown_
  rather than folded into the clean total.

- **Read-only.** It reports; it does not tag, trash or rewrite. What to do about
  a retracted source is a judgement about your own bibliography, and the tool
  that finds it is the wrong place to make that call automatically.

- Twenty-one tests, two mutations watched failing: disabling the syntax rule,
  and letting a missing binary read as clean.

## 0.30.0 — 2026-08-27

Wave 5: where a maintenance run got to, and what it destroyed on the way. The
gap turned out to be narrower than "there is no journal" and worse in one place
than expected.

- **`repair` overwrote a URL with no record of what it was.** This is the find.
  A rewrite changes the Zotero item's URL _and_ the index row, and the previous
  address was recorded nowhere on disk — not in a trash, not in a journal. It
  existed only in the in-memory plan and on stdout. A trashed item sits in
  Zotero's trash and a retired row sits in the retire journal, so a human who
  disagrees can undo those; an overwritten URL simply could not be recovered.
  Every rewrite now journals the URL it is about to replace, before it replaces
  it.

- **`retire` already journalled; `prune`, `repair` and `backfill` did not.**
  Worth stating precisely rather than as "nothing journals". Prune's items land
  in Zotero's trash and are recoverable, but "which of these did that run put
  here" was unanswerable — which is the question you have when a pass surprises
  you.

- **No run of any kind marked its own start or finish**, so an interrupted pass
  left no way to ask where it stopped. The counts printed at the end were the
  only record, and an interrupted run never prints them.

- **A run whose body raises still finishes.** The end record is written with the
  exception named, because a run that died IS finished. Only a process that
  never reached its handlers — SIGKILL, a power cut — leaves an open run. Without
  that distinction every ordinary failure would be reported forever as an
  unexplained interruption, which is exactly the noise 0.15.0–0.18.0 kept having
  to cut back out of the health check.

- **The health check names an unfinished run**, its command, its step count and
  the last thing it touched. Same reasoning as the retry queue in 0.28.0: the
  surest way to have a record nobody acts on is to keep one nobody is told about.

- Append-only JSONL, fsynced per line — a journal that buffers loses precisely
  the tail you needed, the steps closest to the interruption. A torn final line
  is skipped rather than making the whole journal unreadable, because a torn
  write is what an interruption looks like.

- Nine tests, two mutations watched failing: dropping the end record, and
  dropping the before-state from a repair.

## 0.29.0 — 2026-08-27

Wave 4: a captured item stops being just a URL and a title.

- **What the page said when it was read is now recorded.** If a page is edited,
  paywalled or taken down, nothing in the library could show what was actually
  consulted — the one job a provenance record has. A hash does not preserve the
  content, but it turns "this citation might have said anything" into "this
  citation no longer says what it said", which is the difference between a
  reference you can defend and one you can only hope about.

- **The hash covers the COMPLETE document or it is not recorded.** A page over
  5 MB is reported and skipped rather than hashed to its first few megabytes. A
  prefix hash compares equal for two long documents that differ after the cap —
  a false negative in precisely the case the hash exists to catch, a long page
  quietly edited near the end.

- **A verify pass never rewrites a stored hash.** The stored hash is the
  evidence of what was consulted; replacing it with what the page says today
  would destroy the finding at the moment it was made.

- **`extra`, because the live API says so.** `webpage` has no `archive` or
  `archiveLocation` field — checked against `/itemTypeFields?itemType=webpage`
  rather than assumed, and the obvious guess would have been silently dropped by
  Zotero. Other `extra` lines are preserved: it is a field people keep their own
  notes in, and a provenance tool that eats them is not one anybody keeps using.

- **Unattended, never from the Stop hook.** `fetch_title` deliberately stops
  reading at `</title>`, so there is no full body lying around to hash for free,
  and getting one means reading the whole document. The hook's budget is the
  reason the title fetch is capped at a second in the first place.

- **The index is written only after the item is stamped.** Reversed, a refused
  stamp would leave the index claiming a hash that appears nowhere in the
  library, and the next pass would skip the row for having one — a row
  permanently marked done that was never done.

- **Incidental find: `delete_item` had a signature that lied.** Annotated
  `-> None` while returning `False` on both 404 paths, so a successful delete
  and an already-gone item were both falsy and indistinguishable to any caller
  that checked. Nothing checks today — `zotero_setup` ignores the result — which
  is exactly why it was worth correcting before something did. Its comment
  described rewriting a SQLite row and counting a repair, which is `update_url`'s
  story, copied wholesale into a method that deletes. Round 8 fixed this same
  shape elsewhere; this is the second instance.

- Eighteen tests, three mutations watched failing: hashing a prefix instead of
  refusing, letting verify overwrite the stored hash, and writing the index
  before the stamp.

## 0.28.0 — 2026-08-27

Wave 3: the durability floor. A failed Zotero write is no longer lost.

- **A source cited once, which failed once, used to be gone.** Capture has two
  recovery paths and both need the URL to be cited AGAIN: an unissued claim is
  released so a later run can retry it, and an issued one is settled by
  `_resolve_claim` on the next citation. Neither helps a URL nobody mentions
  twice. For a library whose entire purpose is recording what was actually
  consulted, that loss was silent and invisible to the person relying on it.

- **The queue ships with its drain and two bounds.** The README named the reason
  there wasn't one — "a queue nothing drains cannot grow forever" — and that
  reasoning was right, so it is answered rather than ignored. A full queue (500)
  refuses new entries and reports `retry_queue_full` as a capture error rather
  than evicting silently, because dropping the overflow quietly would rebuild
  "logged and dropped" one level up. An entry that has failed 5 times is given
  up on and named, because retrying forever is the failure a bound exists to
  prevent and a sixth attempt from the same command will not succeed where five
  did not.

- **The drain issues no write of its own.** It replays each URL through
  `capture_message` — the same function the Stop hook calls — so it inherits the
  reservation, the claim resolution and the dedup that already exist to answer
  "did that POST commit?". Re-posting blind is how duplicates are made, and a
  test pins that a replayed URL whose first POST did commit is not posted twice.

- **A recovered source keeps its original sighting date.** The queue stores the
  capture's inputs rather than its write, `seen_date` among them. Replaying
  under today's date would file the source under the day the retry ran instead
  of the day it was cited, destroying the one fact the library exists to record.

- **The health check names the queue depth.** The surest way to end up with a
  queue nothing drains is to build one nobody is told about. It is actionable
  and clears itself the moment the drain runs — which is what separates it from
  the health-check noise 0.15.0–0.18.0 kept having to suppress, all of which
  spoke about states the operator could do nothing about.

- Seventeen tests. Three mutations were run and watched failing for their stated
  reasons: removing the queue bound, removing the attempt limit, and dequeueing
  an entry that had failed again.

## 0.27.0 — 2026-08-27

Wave 2: cover the destructive path before building more destructive things. The
coverage gap had a defect sitting in it.

- **Prune said it trashed items it had refused to trash.** Every URL the
  exclusion rules rejected was appended to one list BEFORE the write was
  attempted, and the CLI printed that list as `trashed: <url>`. So an item the
  CAS guard declined — the guard that exists because the collection walk
  finishes minutes before the writes begin, by which time an item may have moved
  — was itemised as removed directly above a summary counting zero:

  ```
    trashed: https://evil.example.com/x
  trashed   : 0
  ```

  Both lines from the same run, reproduced by execution before anything changed.

- **A safety guard that fires in silence is not a safety guard.** `skipped` was
  never printed at all. The operator was told an item had been removed and never
  told that the refusal happened, so there was nothing to prompt them to go look
  at what had changed underneath. `retire_rows.py` has reported its skips all
  along; prune was the one breaking the convention.

- **The list is split by outcome, not by selection.** `trashed_urls`,
  `skipped_urls` and `error_urls` replace the single ambiguous `urls`, which is
  now `selected` and used only for the dry-run listing. There is no longer a
  list meaning "selected, outcome unknown" for a consumer to misread — the fix
  removes the ambiguity rather than guarding it.

- **Rendering moved out of the CLI** into `format_prune_report`, because the CLI
  is where the misreport lived: it kept its own idea of what the result meant,
  and that idea was wrong. Eight tests, three of which were watched failing
  against the regressed code with the original symptom.

  `test_selection_toctou.py` had promised exactly this in its own docstring — "a
  refused delete must not be reported as one" — and asserted it only of the
  counters. One layer up, the itemised list went on breaking that promise for as
  long as the counters stayed right.

## 0.26.0 — 2026-08-27

Wave 1: decide the unknown, then measure. Both answers contradicted something
this project believed.

- **A documented limitation was false.** From its first commit the README said
  sessions bridged with `/remote-control` fire no local `Stop` hook, so "nothing
  is captured in those sessions." It traced back to "Add README" with no
  evidence behind it. Remote Control bridges a session that goes on running
  locally; the hooks documentation says hooks "run wherever Claude Code runs",
  and `CLAUDE_CODE_BRIDGE_SESSION_ID` is set on the LOCAL session while the
  bridge is attached.

  Disproved by execution rather than by reading: a session with that variable
  set captured its own citations, five URLs from one assistant turn, all in the
  library the same day. The real limitation is a different one — a CLOUD session
  does not read local `~/.claude/settings.json`, so a user-scope plugin is not
  installed there at all. The README now says that instead.

- **Every capture records which surface it came from** (`local`, `bridged`,
  `remote`) so the next such claim is a query rather than a belief. The class
  only, never the bridge session id.

- **`dev/measure_coverage.py` answers the question the tests could not.** "Capture
  works" was a happy-path claim: the suite proves a URL in a message reaches
  Zotero and the health check proves the hook ran, but nothing asked what
  fraction of everything actually cited is in the library.

  It reports two numbers, because they mean different things. Coverage of
  ELIGIBLE citations — the final message of a turn, which is what the Stop hook
  is handed — where a miss means capture is broken. And the STRUCTURAL gap, URLs
  cited mid-turn that the design cannot see at all. Conflating them would either
  hide a real failure or invent one.

  Same three rules as its neighbour: the real pipeline (`extract_urls`,
  `canonicalize`, `is_excluded`, the generated-report guard), a window bounded by
  the index's own earliest row, and a `--control` that looks every URL up in an
  empty decoy index where coverage MUST collapse. The byte budget is reported
  rather than silent — a harness that quietly skips the big transcripts reads as
  "I looked at everything".

- **Coverage since 2026-08-26 is 100.0%** (29/29 eligible URLs over 3,001 turns
  and 400 MB of real transcripts, control confirming 0.0% against an empty
  index). Measured across the longer window it is 55.8%, and every miss falls
  before 08-26 — the 29-hour outage when the registry pinned a neutered v0.3.0
  root, already found and fixed. An outage in the window understates current
  health, which is why the window is a flag.

- **The structural gap is empirically ~0**, which was not the expectation. The
  prediction on building this was that agentic turns leak citations from
  intermediate messages; across 3,001 turns almost nothing is cited anywhere but
  the final message. The measurement corrected the intuition, which is the only
  reason to build one.

- **The harness got its own turn boundaries wrong first.** Claude Code records
  tool results as `type: "user"` records, so treating any user record as a
  boundary put every assistant message at the end of its own turn — every
  message eligible, structural gap exactly zero, across a corpus of agentic
  sessions. The number was plausible, which is what made it dangerous; an
  implausible zero was the only tell. Nine tests pin the discriminator now, and
  the first mutation written to check them was itself too weak to fail.

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
