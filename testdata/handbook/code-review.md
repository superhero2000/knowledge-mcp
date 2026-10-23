# Code Review Guidelines

## Why We Review

Code review spreads knowledge, catches defects early and keeps the codebase consistent.
Reviews are about the code, never about the person.

## Size of Changes

Keep pull requests below 400 changed lines. Larger changes must be split, or reviewed in
a scheduled walkthrough session with at least two reviewers.

## Approvals

Every pull request needs one approval from a code owner. Changes to `infra/`, payment code
or authentication code need two approvals, one of which must come from the security
guild.

## Turnaround Time

Reviewers should respond within one business day. If a review is blocking a release
train, mention the reviewer in the `#review-urgent` channel.

## Comment Conventions

Prefix comments to show their weight:

- `nit:` a minor style preference, the author may ignore it
- `question:` the reviewer wants to understand something
- `blocking:` must be resolved before merge

## Automated Checks

The CI runs linting, type checks, unit tests and a dependency licence scan. A pull request
cannot be merged while any required check is red. The licence scan rejects AGPL-licensed
dependencies.
