# Security & Safety Controls
_Last updated: 2026-02-15_

## Token vaulting
- Encrypt tokens at rest (envelope encryption recommended)
- Rotate keys; support re-encryption
- Restrict token access to connector services only

## Least privilege
- Minimize Shopify/Stripe scopes
- Provide NetSuite role templates with least privilege for required objects

## AI tool governance (non-negotiable)
- Tools are the only way the model acts.
- Tool controls:
  - allowlists/denylists
  - default LIMITs and max rows
  - timeouts and rate limiting
  - redaction rules for sensitive fields
- Write tools require:
  - paid entitlement
  - explicit enablement
  - per-action approval

## Threat model highlights
- Prompt injection → treat retrieved text as untrusted; enforce tool gating
- Duplicate posting → idempotency keys + approval + replay protection

## Evidence artifacts
- Evidence packs must be immutable artifacts (object storage) with audit references.
- If attaching evidence into NetSuite, use controlled foldering + unique filenames to avoid overwrites.
