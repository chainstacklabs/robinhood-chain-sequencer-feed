<!--
Title — Conventional Commits, sentence case, imperative: `type(scope): description`.
This repo squash-merges, so the PR title becomes the commit on main. It should say
what the change does to behavior, not which file it touched:

  fix: keep the envelope fields that make a reorg detectable
  docs: correct the claim that a relay verifies its upstream
  build(relay): bump the nitro pin to v3.11.3

Delete any section you have nothing to put in. These comments do not render, so
leave them or delete them as you like. Longer version: .github/CONTRIBUTING.md
-->

## What

<!-- What the code did before, what it does now. Name the function and the file.
     One paragraph, no preamble. -->

## Why

<!-- Why this is a correctness bug rather than a missing feature: the behavior
     someone relies on that is wrong today, and what it costs them.

     Settle it against a source. Nitro source, arbos source, RPC responses, and
     bytecode are evidence — cite them by path and pinned version, e.g.
     `arbos/parse_l2.go` at v3.11.3. Project docs and block explorers are leads,
     not proof. Anything you did not reproduce goes in marked `UNVERIFIED:`. -->

## Changes

<!-- One bullet per behavioral change. Skip this section when What already
     covers a one-line fix. -->

## Verification

<!-- Evidence, not assertions. Paste the output:

     - `uv run pytest` — the count before and after
     - `uvx ruff check src tests` and `uvx ruff format --check src tests`
     - red/green — revert only the fix, keep the new tests, and say which test
       fails and which still pass (a test that fails either way is not pinning
       your change; one that passes either way is not testing it)
     - a live run against the feed when the change touches decoding, transport,
       or the relay image — messages, transactions, sequence range, gaps

     "Tested and works" is not verification. Give the number that changed. -->

## Scope

<!-- What you deliberately left out, and why it belongs in its own PR. Anything
     you found on the way that is out of scope here — say whether it reproduces
     on main. If you picked one of two reasonable designs, name the other and
     offer to switch. -->
