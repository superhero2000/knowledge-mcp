# Security Basics

## Secrets

Secrets must never be committed to the repository. Store them in the "Vault Keeper"
secret manager and reference them by path, for example `vk://team/service/db-password`.
The pre-commit hook `secret-sniffer` blocks commits that look like keys or tokens.

If a secret is leaked, rotate it immediately and report it in `#security-incidents`.
Rotation of a leaked secret is treated as a SEV2 incident.

## Passwords and Keys

Hardware security keys are mandatory for all employees. SMS-based two-factor
authentication is not allowed. SSH keys must be ed25519 and are rotated every 12 months.

## Data Classification

Data is classified as Public, Internal, Confidential or Restricted. Customer location
data from buoys is Confidential. Restricted data (payment details) may only be processed
in the isolated "Cove" environment.

## Laptops

Laptops must have full-disk encryption enabled and lock after 5 minutes of inactivity.
Lost or stolen laptops must be reported to IT within 24 hours.
