# Shopify Integration
_Last updated: 2026-02-15_

## Auth
- Shopify OAuth; store tokens encrypted.
- Minimize scopes required for reconciliation and visibility.

## Data needed (MVP)
- Orders, refunds, transactions required for payout reconciliation
- Start with polling; add webhooks for freshness later

## Rate limiting
- Backoff and retry
- Cache incremental sync cursors per shop
