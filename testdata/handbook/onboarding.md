---
title: Onboarding Guide
---
# Onboarding Guide

Welcome to Brightwater Labs. This guide covers your first two weeks.

## First Day

You receive a laptop pre-provisioned with the standard developer image "driftwood-24".
Collect your hardware security key from the front desk; it is required for every login to
internal systems. Your buddy will walk you through the office and introduce the team.

## Accounts and Access

Request access to the source code host through the self-service portal "Harbor".
Access requests are approved by your team lead within one business day.
Production access is never granted in the first 30 days. After 30 days, production read
access requires completing the "Safe Operations" course.

## Development Environment

Clone the monorepo and run `make bootstrap`. The bootstrap script installs Python 3.12,
Node 20 and the internal CLI called `bw`. If `make bootstrap` fails with a certificate
error, run `bw certs refresh` and try again.

### Editor Settings

We recommend VS Code with the shared settings in `tools/editor/settings.json`.
Format on save is mandatory; the formatter for Python is ruff and for TypeScript is prettier.

## Your First Week

During the first week you pair with your buddy on at least three small tickets labelled
`good-first-issue`. On Friday you present what you learned in the team demo (5 minutes).

## Expense Policy for New Hires

New hires can expense a home-office budget of 400 euros during their first 90 days.
Submit receipts through Harbor under "Expenses > Home office".
