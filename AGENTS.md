# llvm-dev

LLVM workspace. `llvm-project/` is the checkout (`origin` = fork, `upstream` = llvm); the
`justfile` drives builds and tests (`just` lists recipes; don't call cmake, ninja or lit
directly).

## Compiler work

- LangRef is the spec. A transform is correct only if no input makes the result more
  poisonous, undefined or trapping than the original; name the semantics behind a claim.
- Reproduce with this worktree's `opt` before theorizing. The main checkout's build is the
  "before" (`just before opt …`); the `godbolt` MCP runs upstream trunk.
- Prove IR changes with the `alive2` MCP (`just alive verify` in a shell), then try to
  break the fix: other flags, operands, branches, vectors, multiple uses. Alive2 is
  bounded and intraprocedural.
- Find which pass changed the IR, and why, with `opt -print-changed=diff
  -filter-print-funcs=<fn>`, `-opt-bisect-limit=N` and `-debug-only=<pass>`. Shrink a
  reproducer with `just reduce <passes> <file>`.
- Fix the root cause with the pass's existing helpers; report anything wider separately.
- Commit the test first, with CHECK lines from the unfixed compiler via
  `llvm/utils/update_test_checks.py --version 5`; keep every RUN configuration.
- Run `just stale <tools>` before trusting a test failure or `opt` output.
- Say what you ran versus what you infer.

## Upstream

Never push, comment, open a PR or run `just alive link` without the user's OK. No AI
fixes for `good first issue`; disclose AI help with an `Assisted-by:` trailer.
