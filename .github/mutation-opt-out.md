Opted out 2026-09-14: 32 of this repo's test files read the source text of
`scripts/` (via `read_text()` / `ast.parse`) to assert structural rules such as
"every row filter reaches every snapshot() call". mutmut rewrites that source
with trampolines, so those assertions fail inside `mutants/` before a single
mutant is checked. Mutation testing needs the structural tests split from the
behavioural ones first; until then this file keeps the nightly job honest
instead of green-by-abort (see repo-template nightly-guardrails.yml).
