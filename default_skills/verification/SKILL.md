---
name: verification
description: Establishing that a change actually works — baselines, targeted vs full runs, reading the exit code instead of assuming, and who is allowed to judge the result. Load before claiming a change is done, not while writing tests.
version: 1.0.0
triggers:
  - verify
  - does it work
  - run the tests
  - check my change
  - is it fixed
  - regression
---

# Verification

This is about establishing that a change works. It is not about producing more
tests: writing tests is cheap to do and easy to do uselessly, and the volume of
them is not what decides whether the work is correct.

## Take a baseline first

Run the relevant tests **before** you change anything, and keep the result.

Without a baseline, a red run after your edit is ambiguous — you cannot tell
your breakage from breakage that was already there, and the usual reaction is
to "fix" a test that was never yours. A green run is just as ambiguous: it may
mean nothing in that suite covers the code you touched.

Record what was already failing, by name. That list is what "unchanged" means
at the end.

## Run the smallest thing that answers the question

- Iterate with the targeted subset that covers what you changed. Full suites
  cost minutes; a module's tests usually cost under a second.
- Run the full suite **once**, before you report done — enough to catch what
  your targeted subset didn't cover, without paying for it every edit.
- If you don't know which tests cover the code you changed, find out (grep the
  test files for the module name) rather than defaulting to the full run.

## Read the result

A run that finished is not a run that passed.

- Read the **exit code**. Then read the summary line. A suite can print
  hundreds of lines of progress and still end in failure.
- Compare against the baseline by name. "Same two failures as before" is a
  pass; "two failures" on its own is not a finding.
- Never report success from the fact that you executed the command. If you did
  not read the outcome, you have not verified anything — say so instead.
- A test that errors during collection or import never ran. It is not a pass.

## When it's red

- Read the actual assertion and the actual values before changing anything.
- Fix the code the test is describing. Change the test only when the test is
  the thing that's wrong — a renamed symbol, a reworded string, a contract you
  deliberately changed — and say plainly in your summary that you changed it
  and why.
- **Never** weaken an assertion, delete one, mark a test skipped, or loosen a
  comparison to make a failure disappear. That converts a caught bug into an
  uncaught one, and it is invisible in a green run. If you cannot make it pass
  honestly, leave it failing and report it.
- Do not "fix" a failure that predates your change unless that is the task.

## Test what the code does, not how it does it

- Assert on observable behaviour: return values, raised errors, files written,
  state after the call. Tests bound to internal call sequences or private
  helpers break on every refactor while catching nothing.
- Prefer one test that fails for a real reason over five that restate the
  implementation.
- The defects in generated code cluster in places happy-path tests never
  reach: error handling, edge and boundary values, concurrent access, and the
  failure modes of anything external — timeouts, refused connections, malformed
  responses, rate limits, partial writes. That is where a new test earns its
  place.
- A test that would still pass if you deleted the code under it is not a test.

## You are not a reliable judge of your own output

The reasoning that wrote the code carries the same blind spots into judging it:
if you misread the requirement, your implementation and your verification of it
are wrong in the same direction and agree with each other.

So make the verifier something other than the writing pass:

- Prefer an **executable** check over your own reading — the test run, the
  actual command, the real output. Evidence you did not produce yourself.
- For a substantial change, hand the review to a fresh context: `spawn_chain`
  with a verify step, a sub-agent given only the diff and the requirement, or
  `review` mode. Do not hand it your reasoning as well; give it the artifact
  and let it form its own view.
- Re-reading your own diff is the weakest check available. It is worth
  something, but never report it as verification.

## Reporting

State what you ran, and what it said. "664 tests, the same 2 pre-existing
failures" is a result; "tests pass" is a claim. If part of the change is
unverified — no coverage, needs a real device, needs credentials you don't
have — say which part and why, rather than letting a green run imply more than
it proved.
