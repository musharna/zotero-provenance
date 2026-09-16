# Lessons

One line per miss: date, the class of miss, the mechanism that now catches it.

- 2026-09-16 (fuzz #14) A "never block a turn" guard whose `try` began after work that could raise, and three pure parsers whose stdlib calls raise on input their contract promised to answer (`Path.exists` on ENAMETOOLONG, `inet_aton` on NUL, `int(x, 16)` on a non-digest). No lint can type-infer `Path.exists` or tell a guard's scope from its intent, so no hook was added; the weekly `fuzz` job caught it, and each case now has a seen-failing test with a positive control.
- 2026-09-16 (PR #17 review) "Each test was seen to fail" was true per test and false per assertion: a test stops at its first failing assert, so a NUL assertion placed after it never ran against the old code, and it passes there too. Its claim went into a docstring and this file. Caught by the CI `review` job; a multi-case fix now checks each case against the old code on its own, not only the test as a whole.
