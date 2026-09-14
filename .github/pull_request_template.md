<!--
Title: Conventional Commits, `type(scope): description`. It becomes the commit on
main, so say what changes in behavior, not which file you touched:

  fix: keep the envelope fields that make a reorg detectable
  docs: correct the claim that a relay verifies its upstream
  build(relay): bump the nitro pin to v3.11.3

Comments don't render. Delete a section you have nothing to put in.
Rules: .github/CONTRIBUTING.md
-->

## What

<!-- What it did before, what it does now. Name the function and file. -->

## Why

<!-- What is wrong or missing today, and what it costs someone. Settle it against
     a source: nitro or arbos source at a pinned version, RPC responses, bytecode.
     Docs and explorers are leads, not proof. Mark anything you did not reproduce
     `UNVERIFIED:`. -->

## Verification

<!-- Paste output, not "tested and works":

     - the checks from CONTRIBUTING.md, with the test count before and after
     - red/green: revert the fix, keep the tests, name the test that fails
     - a live feed run when the change touches decoding, transport, or the relay
       image: messages, transactions, sequence range, gaps

     Every number here came from a command that ran on this branch. -->

## Scope

<!-- What you left out and why it is its own PR. Anything found on the way that is
     out of scope, and whether it reproduces on main. If two designs were
     reasonable, name the one you didn't take. -->
