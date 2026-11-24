---
id: internal-fee-refunds-goodwill
title: "Internal: fee refunds and goodwill limits"
product: fees
visibility: internal
intents: [fees_fx]
updated_at: 2026-11-24
---
## The rule

Fees are never refunded by the AI. The AI must not promise or suggest a fee refund. A fee refund is always a decision of a human operator.

## Goodwill limit

- An operator may refund fees up to **€25 per case** as goodwill.
- Above €25 per case, a supervisor must approve the refund.

Do not tell the customer the goodwill limit or that a supervisor approval exists.

## Before you decide

1. Check the fee against the help-centre article "Tallybridge fees overview".
2. Check the currency rule. Fixed fees are set in EUR and taken from a USD or GBP balance at the mid-market rate without the conversion fee. A SWIFT withdrawal in USD costs €15 taken from the USD balance, not "15 USD". A customer who sees a USD amount for the SWIFT fee has usually not been overcharged.
3. Check failed and returned withdrawals. A failed or returned withdrawal costs nothing. If a withdrawal fee was charged on a failed or returned withdrawal, the fee was charged against the rules: refund it and open a Jira bug.
4. Check the inactivity fee. It is €5 per month after 12 months without transactions and never takes the balance below zero. A balance below zero because of this fee is a bug.

## Replacement cards

When a lost or stolen card is replaced, the replacement is a new delivery and the delivery fee applies (€9.90 standard, €24.90 express). Waiving it is a goodwill refund and follows the €25 rule.

Replacing a card that was never delivered is free. No fee is charged, so there is nothing to waive or refund.

## What to tell the customer

- If you refund: confirm the amount and the currency.
- If you do not refund: explain the fee with the help-centre article. Offer a formal complaint if the customer is not satisfied. A complaint gets a written final answer within 15 business days.

## Audit

Every refund decision is recorded in the ticket. Write the fee, the amount refunded and, above €25, the name of the approving supervisor.

Internal reference: CANARY-INT-LARCH.
