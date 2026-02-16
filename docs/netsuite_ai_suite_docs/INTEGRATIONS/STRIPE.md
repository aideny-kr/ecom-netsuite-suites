# Stripe Integration
_Last updated: 2026-02-15_

## Auth
- OAuth where appropriate; store access/refresh tokens encrypted.

## Data needed (MVP)
- Payouts
- Balance transactions (fees, refunds, adjustments)
- Disputes/chargebacks (read-only for variance taxonomy)

## Reliability requirements
- Idempotent write behavior on any Stripe-triggered downstream actions.
