# PR #32 review, round 3: the fixes for round 2

**Verdict: approve.** All six round-2 fixes in `ec7ccb2` hold (M1–M3, L1–L3). There are no Critical, High or Medium
issues, and three Lows that don't block the merge. Validation passes. The `code-reviewer` agent did the deep pass in a
clean context, because this session wrote the fixes. It could only read code, so every httpx behaviour it relied on
was then confirmed by running it on the PR head (see *Checked by running*).

L4 (the street-number-first match against live listings) is deferred to the Level 4 live run by the owner and logged
in `_tasks/todo.md`.

## Validation (head `ec7ccb2`)

| Check | Result |
|---|---|
| ruff check / format | ✅ clean / 181 files formatted |
| mypy strict | ✅ 183 files, no issues |
| pyright strict | ✅ 0 errors |
| pytest (DB tier, port 5436) | ✅ 1,304 passed, 2 skipped (paid live eval) |
| pytest (no DB) | ✅ 943 passed, 363 skipped |
| alembic check (test DB) | ✅ no drift; no migration |

## Round-2 findings: closed

- **M1:** `_has_alphabetic_tld` refuses every address the resolver accepts (dotted, short, hex, octal), because each
  one's last part starts with a digit. Trailing dots are stripped first, so `a@1.2.3.4.` is refused. Unicode and `xn--`
  TLDs pass. No real domain is wrongly refused.
- **M2:** `_imports_from` resolves level-1 and level-2 relative imports and `__init__.py` packages correctly, and
  matches the package form and aliases. The old matcher misses all four new spellings. The real importer,
  `verify_business.py`, still matches.
- **M3:** the README and both docstrings now match `_same_name` (`min(len, 2)`), and the one-word limit is pinned by
  a test.
- **L1:** `{None, 443}` is exact, because httpx normalises `:443` to `None`. The port test fails on the pre-fix code.
- **L2:** hosts are compared as `raw_host`. A `%` host is `bad_url` on the first hop and `other_host` on any later
  one. A trailing-dot redirect is refused as `other_host`, which is conservative.
- **L3:** `next(...)` cannot raise. `_checkable` tests the same two names with the same `_name_tokens`, so passing it
  guarantees a name with tokens.

## Checked by running (on `ec7ccb2`)

- `httpx.URL("https://x.test:443/").port` → `None`; `"https://ACME.Test/"` → `raw_host` `b"acme.test"`.
- `"https://bücher.de/"` → `raw_host` `b"xn--bcher-kva.de"`, and `str()` keeps the encoded host.
- `HomepageChecker.check("bücher.de")` against a page at `https://xn--bcher-kva.de/` → found. The origin
  `https://xn--bcher-kva.de` and `source_url` `https://xn--bcher-kva.de/` agree.
- `email_domain("a@1.2.3.4.")` → `None`; `email_domain("a@bücher.de")` → `bücher.de`.

## Issues

### Low

- **L1. The import guard's remaining blind spots aren't written down.** `tests/test_structure.py:278-291`
  - `from app.sourcing.sources import *` pulls in `PlaceResult`, because `google_places.py` has no `__all__`. The alias
    is `*`, so the match fails.
  - `from app.sourcing import sources` followed by `sources.google_places` is also missed.
  - Neither is plausible in this codebase, which has no star imports and no attribute-path access to adapters.
  - **Fix:** name both in the docstring as known limits. Optionally, match `*` when the source is the adapter's
    parent package.
- **L2. The malformed-domain test doesn't pin its reason.** `tests/sourcing/test_website.py:138-141`
  - It asserts one logged reason, but not that the reason is `bad_url` or that nothing was requested.
  - It still fails on the pre-fix code, because the unrouted mock raises. But it would also pass for any other
    refusal.
  - **Fix:** assert `_reasons(logs) == ["bad_url"]` and `not web.requests`.
- **L3. The Unicode-domain path is untested.** `tests/sourcing/test_website.py`
  - Only the `xn--` form is tested. The census can state `bücher.de`, which works (see above) but nothing pins it.
  - **Fix:** add `_check(web, domain="bücher.de")` against `html("https://xn--bcher-kva.de/")`, and assert that
    `value` and `source_url` are punycode.

## Done well

- Each fix has a test that fails on the pre-fix code, and the import matcher is checked spelling by spelling,
  including the near misses.
- The docs state the one-word limit instead of overclaiming it.
- Refusing a `%` host before any request keeps "an unparseable domain is never requested" true after the switch to
  `raw_host`.
- The fence docstring now matches the code: port 443 only, and it still names the DNS-to-private-address case it
  does not fence.

## Recommendation

Merge once freight v9 is active (`dry-run`, then `activate … --accept-terms …,places`). L1–L3 are a few lines of
tests and docs. Fold them in now, or carry them to T6 PR B.
