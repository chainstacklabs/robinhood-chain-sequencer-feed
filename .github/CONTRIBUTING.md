# Contributing

This is a research repo that ships as a package. The one rule that covers both:
a claim in the README or a branch in the decoder has to be reproducible, and the
PR is where you show it.

## Before you open a PR

- Branch from `main`, one concern per branch. Docs and behavior changes are
  separate PRs.
- Name it `type/short-description`, for example `fix/verify-is-mainnet-only`.
- Add a test that fails without your change.
- Run the checks:

  ```bash
  uv sync --extra dev
  uv run pytest
  uv run ruff check src tests
  uv run ruff format --check src tests
  ```

## The PR

Fill in the [template](pull_request_template.md). It carries the rules as
comments, so they travel with the PR body. Two of them matter most:

- **Title is the commit.** The repo squash-merges, so the title lands on `main`
  as is. Conventional Commits, describe the behavior change:
  `fix: keep the envelope fields that make a reorg detectable`.
- **Evidence over assertion.** RPC responses, bytecode, and source at a pinned
  version settle a question. Project docs and block explorers only point at one.
  Anything you did not reproduce yourself is marked `UNVERIFIED:` inline.
  `MAINNET_SIGNER` was recovered from live messages and confirmed against the L1
  `SequencerInbox`; every constant next to it is held to the same bar.

Numbers in the README earn their place by justifying a design decision.
Benchmarks belong in the PR that used them, not in the README.
