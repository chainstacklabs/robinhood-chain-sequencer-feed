# Contributing

This is a research repo that ships as a package. The bar is the same for both: a
claim in the README or a branch in the decoder has to be reproducible, and the PR
is where you show it.

## Before you open a PR

- Branch from `main`. One concern per branch — if two fixes touch no overlapping
  lines, they are two PRs, and each says so.
- Name the branch `type/short-description`: `fix/verify-is-mainnet-only`,
  `docs/correct-relay-verification-claims`, `chore/refresh-dependencies`.
- Run the suite and the linter:

  ```bash
  uv run pytest
  uvx ruff check src tests
  uvx ruff format --check src tests
  ```

- Add tests that fail without your change. A test that passes on `main` is not
  testing your change.

## The PR

The template in `pull_request_template.md` is the structure — what, why,
changes, verification, scope. The parts that matter:

- **Title.** Conventional Commits, sentence case: `type(scope): description`.
  The repo squash-merges, so this line becomes the commit on `main`. Describe
  the behavior change, not the file.
- **Evidence over assertion.** RPC responses, source at a pinned version, and
  bytecode settle questions. Project docs and block explorers point at them.
  Cite the file and the version you read — `arbos/parse_l2.go` at nitro v3.11.3,
  not "nitro caps it somewhere." Anything you did not reproduce yourself is
  marked `UNVERIFIED:` inline rather than left to look verified.
- **Numbers.** Paste the test count before and after, the live run, the
  measurement. A number without a command that produced it does not count.
- **Scope.** Say what you left out. A fix that also rewrites four paragraphs of
  README is two PRs. Findings that are real but out of scope go at the end of the
  PR, with whether they reproduce on `main`.
- **Alternatives.** Where two designs were both reasonable, name the one you
  didn't take and offer to switch. Reviewers should not have to guess whether
  you considered it.

## What does not land

- A constant you guessed. `MAINNET_SIGNER` was recovered from real messages and
  confirmed against the L1 `SequencerInbox`; anything sitting next to it is held
  to that.
- A measurement in the README that doesn't justify a design decision. Benchmarks
  belong in the PR that used them.
- A behavior change inside a docs PR, or the reverse.

## Working with agents

Coding agents can fill the template directly — it carries the rules in HTML
comments so they travel with the PR body. The one thing to check before you
submit: every number in the body came from a command that actually ran, in this
branch, and not from the model's recollection of an earlier one.
