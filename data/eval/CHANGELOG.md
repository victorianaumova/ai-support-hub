# Changelog of the evaluation data

Label fixes in `tickets.dev.jsonl` (eval plan §7). Newest first.

## 2026-11-24 (P12, label review)

- **T-0005.** Dropped key fact 3 (check the bank details and create the withdrawal again): the history already says the customer checked them.
- **T-0008.** Key fact 3 now states only the pending payments (8,950.00 EUR and 4,880.00 EUR, TX-000124, TX-000125), without the adding-up conclusion; the writer must not state a cause. Fact 2 is reachable through `account-limits`.
- **T-0015.** Intent stays `login_security` (world §9: suspicious activity; it is the only `login_security` ticket); `secondary_intent` is now `card_lost_stolen` with tag `two_intents`, because the evidence is two card payments and the first step is to freeze the card. Gold documents gain `card-lost-or-stolen`. Flags, `must_handoff` and `expected_reason` unchanged.

## 2026-11-20 (P7-07, owner decision)

- **T-0002, T-0007, T-0020, key fact 1.** Removed the merchant names (Saltmarsh Games Ltd, CoinHarbor Exchange, Aerolume Travel). They appear only in the customer's message, which the writer must not restate (write prompt, "Stay with the sources"), so the key fact could not be reached. Amount, currency, reference and error code stay; amounts are from the seed transactions.
