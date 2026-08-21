# Restock Create Failure Fallback

## Problem

A provider can pass the balance and inventory probes but reject the subsequent
order creation with a definitive business error. The local order remains in
`creating` without an `order_id`, so every patrol retries the same provider and
never reaches the next enabled provider.

The same stale state can remain after an operator disables that provider.

## Design

- Before creating an order without an `order_id`, skip its provider when that
  provider is no longer enabled and schedule the next enabled provider.
- When order creation returns a definitive business failure, including HTTP 402
  insufficient balance, schedule the next enabled provider through the existing
  follow-up state transition.
- Preserve the existing idempotency key and retry the same provider for network
  failures or other ambiguous outcomes. This avoids duplicate remote orders.
- Preserve the requested quantity and configuration snapshot during fallback.
- Record a provider fallback action and transition reason in the normal order
  history and live log.

## Verification

- A disabled provider with a local `creating` order is never called and moves to
  the next enabled provider.
- An HTTP 402 create failure moves to the next enabled provider.
- A network failure leaves the current provider and idempotency key unchanged.
- Existing order polling, partial delivery, recovery, and pool push tests remain
  green.
