# Codex repository guidance

## Review guidelines

Thoroughly review every pull request before it is merged.

Inspect the complete affected code paths, their callers, configuration files,
state transitions, cleanup paths, and related functions. Do not limit review to
the individual changed lines. Check likely edge cases and failure conditions;
do not assume code is correct merely because the normal path succeeds.

Report only actionable, high-confidence findings caused by or exposed by the
pull request. Do not report unrelated pre-existing issues unless the change
makes them materially more dangerous. Do not report style-only preferences as
defects.

Treat the following as P1 issues when they could materially affect correctness,
security, data integrity, compatibility, result completeness, or resource
safety:

- Incorrect behavior or regressions.
- Data loss, overwritten files, unsafe deletion, or incomplete rollback.
- Security vulnerabilities, including command injection, path traversal,
  server-side request forgery, and unsafe URL handling.
- Exposed passwords, tokens, API keys, cookies, credentials, or personal
  information.
- Infinite loops, crawl loops, runaway recursion, or uncontrolled CPU, memory,
  disk, file-descriptor, or network use.
- Missing timeouts, bounded retries, input validation, exception handling, or
  cleanup where their absence can cause a material failure.
- Incorrect URL parsing or normalization, redirect handling, origin or path
  scoping, filtering, or duplicate detection.
- Broken concurrency, locking, SQLite state, queue transitions, interruption,
  resume, recrawl, or recovery behavior.
- Changes that silently skip pages, files, sitemap entries, or other expected
  results.
- Cross-platform or supported-Python compatibility problems.
- Missing or inadequate tests for new or changed nontrivial behavior.
- Documentation, help text, defaults, or configuration examples that no longer
  match the implementation.

For every finding:

- Identify the affected file and the smallest useful line range.
- Explain the incorrect behavior and the conditions that trigger it.
- Describe the concrete impact and why the severity is justified.
- Recommend the safest minimal correction.
- Identify the regression test or validation needed to prove the correction.

Review error paths as carefully as success paths. Consider malformed and
adversarial inputs, partial responses, redirects, interrupted writes, stale
locks, corrupt or old state, network failures, permission errors, boundary
values, and cleanup failures when relevant.

Do not approve a pull request while a known P0 or P1 issue remains unresolved.
After fixes are pushed, review the complete updated diff and check that the
fixes did not introduce regressions or leave required tests and documentation
behind.
