# The Tallybridge world

Tallybridge is a **fictional** company. It does not exist and has no relation to any real company. All numbers in this file are invented for this project. They are the single source of facts for the knowledge base (`data/kb/`), the seed data (`data/seed/`) and the evaluation tickets (`data/eval/`). If a fact is not in this file, a knowledge-base article must not state it.

## 1. Company

- Cross-border payments platform for freelancers and small businesses. Similar in idea to Payoneer or Wise Business.
- Licensed in the EU as an electronic money institution (fictional licence). Founded in 2017.
- 180,000 active customers in 60 countries. Most customers receive money from foreign clients and marketplaces, then withdraw it to a local bank or spend it with a card.
- Customer-facing languages: English only (MVP assumption).

## 2. Products

| Product | What it does |
|---|---|
| **Multi-currency Account** | Balances in EUR, USD and GBP. The customer gets local receiving details: EUR IBAN (SEPA), USD account and routing number (ACH), GBP sort code and account number (Faster Payments). |
| **Payment Requests** | The customer sends a payment link or invoice. The payer pays by card or bank transfer. |
| **Withdrawals** | Move money from the Tallybridge balance to the customer's own bank account. SEPA for EUR in the EU, SWIFT for everything else. |
| **Tallybridge Card** | Mastercard debit card, virtual and physical. Spends from the balance. |
| **Mass Payouts** | Business tier only. Pay up to 500 recipients with one file. |

## 3. Account tiers and limits

| | Starter | Verified | Business |
|---|---|---|---|
| Verification | ID document + selfie | Starter checks + proof of address + source of income | company documents + owners (KYB) |
| Receive per month | €1,000 | €30,000 | €250,000 |
| Withdraw per day | €500 | €10,000 | €50,000 |
| Withdraw per month | €1,000 | €30,000 | €250,000 |
| Card | not available | 1 physical + 3 virtual | 5 physical + 20 virtual |
| Card spend per day | — | €5,000 | €15,000 |
| ATM withdrawal per day | — | €1,000 | €2,000 |
| Mass Payouts | no | no | yes |

- An account without a finished identity check cannot receive, hold or send money (EU anti-money-laundering rules). The customer can only log in and upload documents. Verification decisions are made by the Compliance team, never by the AI.
- Limits are in EUR equivalent. They reset at 00:00 CET (daily) and on the 1st day of the month (monthly).
- Pending card payments count toward the daily card spend limit.
- A limit increase is possible only for Verified and Business. The customer sends a request with proof of income. Review time: 3 business days. A human in the Compliance team decides. The AI must not promise an increase.

## 4. Fees

| Operation | Fee |
|---|---|
| Receive EUR by SEPA | free |
| Receive GBP by Faster Payments | free |
| Receive USD by ACH | 1% of the amount |
| Payment Request paid by card | 2.9% + €0.30 |
| Payment Request paid by bank transfer | 1%, maximum €10 |
| Withdrawal by SEPA | €1.50 |
| Withdrawal by SWIFT | €15 |
| Currency conversion | mid-market rate + 2.0% (Business: + 1.5%) |
| Virtual card | free |
| Physical card delivery | €9.90 (standard), €24.90 (express) |
| Card annual fee | €24, charged from the second year |
| ATM withdrawal | €2.50 per withdrawal |
| Card payment in a currency you do not hold | conversion fee as above |
| Inactivity fee | €5 per month after 12 months without transactions; never takes the balance below zero |
| Account opening and closing | free |

How fees are charged:
- Fixed fees (the amounts in EUR above: withdrawals, card delivery, annual card fee, ATM, inactivity) are set in EUR. When they are taken from a USD or GBP balance, the EUR amount is converted at the mid-market rate, **without** the conversion fee. A SWIFT withdrawal in USD costs €15 taken from the USD balance, not "15 USD".
- Percentage fees (receiving USD by ACH, Payment Requests, currency conversion) are charged in the currency of the payment.
- A failed or returned withdrawal costs nothing: the withdrawal fee is not charged.
- Mass Payouts have no fee (a Business tier benefit).
- A card payment is made in the merchant's currency. If the customer does not hold that currency, the amount is converted with the conversion fee above.

Fees are never refunded by the AI. A fee refund is a decision of a human operator (goodwill limit: €25 per case; above that a supervisor approves).

## 5. Timeframes

| Event | Normal time |
|---|---|
| Incoming SEPA transfer | 1 business day; SEPA Instant: under 1 minute |
| Incoming ACH (USD) | 1–3 business days |
| Incoming Faster Payments (GBP) | same day |
| Payment Request paid by card | visible at once as *pending*; available after 3 business days (new accounts: 7 calendar days for the first 3 payments) |
| Payment held for review | up to 3 business days; we may ask for an invoice or contract |
| Withdrawal by SEPA | 1 business day if requested before 15:00 CET |
| Withdrawal by SWIFT | 2–5 business days |
| Failed withdrawal returned to balance | 3–7 business days |
| KYC review (Starter, Verified) | 1–2 business days |
| KYB review (Business) | 3–5 business days |
| Physical card delivery | 7–14 business days in the EU; up to 21 business days elsewhere; express 3–5 business days (EU only) |
| Card dispute window | 120 days from the transaction date |
| Card dispute decision | up to 45 days; complex cases up to 90 days |
| Merchant refund to card | 5–10 business days |
| Account closure | 30 days after the request; the balance must be withdrawn first |

Business days are Monday–Friday without the closing days of the euro payment system (TARGET calendar). "CET" in this file means Central European Time, including summer time (CEST).

## 6. Security and compliance rules

- Two-factor authentication (2FA) is mandatory: authenticator app or SMS.
- 5 wrong passwords lock login for 30 minutes. 3 wrong card PINs block the card PIN; the customer unblocks it in the app.
- The customer can **freeze and unfreeze** a card in the app at any time. A lost or stolen card must be frozen at once and then replaced (replacement = new delivery fee).
- Replacing a card that was never delivered is free (owner decision, P12 data PR).
- Tallybridge staff never ask for a full card number, CVV, PIN, password or 2FA code. Not in chat, not by email, not by phone.
- **Account restrictions.** Compliance can restrict an account (no outgoing payments) during a review. Staff and AI must **not** tell the customer the reason or the details of the review (legal "tipping-off" rule). The only allowed message: the account is under review, the team may contact the customer for documents, typical time is up to 10 business days.
- Unauthorised transactions: the customer must report them within 13 months. Support freezes the card and opens a fraud case. Only humans handle fraud cases.
- Formal complaints get a written final answer within 15 business days (EU payment-services rule). Only humans handle complaints.
- Supported ID documents: passport, national ID card, driving licence (EU/UK only). Proof of address: utility bill or bank statement not older than 3 months.
- Countries where Tallybridge does not open accounts: countries under EU sanctions. No customer text mentions sanctions: it says only that Tallybridge cannot open or serve accounts in some countries, with no names (owner decision, P2-08).

## 7. Error codes

Error codes are shown in the app as `ERR-####`. The full list (about 40 codes) is in `data/kb/error_codes.csv`. Ranges:

| Range | Area | Examples |
|---|---|---|
| ERR-1xxx | incoming payments | ERR-1004 sender name does not match; ERR-1012 payment held for review; ERR-1020 monthly receive limit reached |
| ERR-2xxx | withdrawals | ERR-2001 daily limit reached; ERR-2003 beneficiary bank rejected; ERR-2010 IBAN invalid; ERR-2015 name of beneficiary does not match account holder |
| ERR-3xxx | card | ERR-3001 insufficient balance; ERR-3002 card frozen; ERR-3005 merchant category blocked; ERR-3009 3-D Secure failed; ERR-3011 daily spend limit reached |
| ERR-4xxx | verification | ERR-4001 document unreadable; ERR-4002 document expired; ERR-4006 selfie does not match; ERR-4010 proof of address too old |
| ERR-5xxx | account and login | ERR-5001 login locked; ERR-5003 2FA code invalid; ERR-5010 account restricted |

Withdrawals are allowed only to a bank account **in the customer's own name** (ERR-2015). Blocked merchant categories for the card: gambling, crypto exchanges, adult content.

The other codes of the full list (owner-approved in P2-08). Each one follows from a fact above; the code number is only an identifier. Mass Payouts use the ERR-2xxx range.

| Code | Meaning | Based on |
|---|---|---|
| ERR-1001 | currency not supported (only EUR, USD and GBP can be received) | §2 |
| ERR-1008 | account not verified, so the payment cannot be received | §3 |
| ERR-2002 | monthly withdrawal limit reached | §3 |
| ERR-2004 | balance too low for the withdrawal and its fee | §4 |
| ERR-2011 | SWIFT/BIC details invalid | §2 |
| ERR-2012 | SEPA not available for this currency or country (SEPA is for EUR in the EU) | §2 |
| ERR-2020 | withdrawal returned by the receiving bank | §4, §5 |
| ERR-2030 | Mass Payouts need the Business tier | §2, §3 |
| ERR-2031 | mass payout file has more than 500 recipients | §2 |
| ERR-3003 | card PIN blocked after 3 wrong PINs | §6 |
| ERR-3004 | card cancelled | §6 |
| ERR-3007 | wrong PIN | §6 |
| ERR-3012 | daily ATM withdrawal limit reached | §3 |
| ERR-4003 | ID document type not supported | §6 |
| ERR-4004 | driving licence not from the EU or UK | §6 |
| ERR-4011 | proof of address is not a utility bill or bank statement | §6 |
| ERR-4012 | proof of source of income missing | §3 |
| ERR-4015 | company documents or owner details incomplete (KYB) | §3 |
| ERR-4020 | country not supported: Tallybridge cannot open or serve accounts there (the customer text does not mention sanctions) | §6 |
| ERR-5002 | wrong password | §6 |
| ERR-5004 | 2FA not set up | §6 |
| ERR-5011 | identity check not finished: the customer can only log in and upload documents | §3 |
| ERR-5012 | account cannot be closed while it has a balance | §5 |
| ERR-5020 | limit increase not available on the Starter tier | §3 |

All codes are shown to customers. ERR-5010 (account restricted) uses only the allowed message of §6, never a reason.

## 8. Support today (before the AI Hub)

| Fact | Value |
|---|---|
| Channels | in-app chat (70% of tickets), email (30%) |
| Tickets per day | 1,200 on average; 2,000 on peak days (start of month, incidents) |
| Operators | 38, two shifts |
| Human support hours | Mon–Fri 08:00–20:00 CET, Sat 10:00–16:00 CET |
| Median first response | 3 h 40 min |
| Average handling time | 9 minutes per ticket |
| Cost per human-handled ticket | €4.50 (**assumption**; salary + tools + management) |
| Customer satisfaction (CSAT) | 78% |
| Urgent cases outside support hours | an on-call fraud analyst reads `#support-escalations`; target: first human response within 60 minutes |
| Human SLA after the Hub is live | standard: first human response within 4 business hours; urgent (fraud, lost card, restricted account): within 30 minutes |
| AI notice after the Hub is live | every chat starts with: "You are chatting with Tallybridge's AI assistant. It can make mistakes. You can ask for a human at any time." ([security](security.md) §7) |

Tools: Jira Service Management for cases that need work by another team (disputes, fraud, bugs, limit requests). Slack channel `#support-escalations` for urgent cases. A real company of this size also has a helpdesk product (for example Zendesk or Intercom); in this project the Hub is its own small helpdesk, so that the demo is complete without a paid product.

## 9. Why customers write — the top reasons

| # | Reason | Share | Intent label | Can AI answer alone? |
|---|---|---|---|---|
| 1 | Incoming payment has not arrived or is pending | 18% | `payment_pending` | Often: explain timeframes, check the transaction status |
| 2 | Verification: which documents, why rejected, how long | 14% | `kyc_verification` | Often |
| 3 | Withdrawal is late or failed | 13% | `withdrawal_issue` | Often: status + error code |
| 4 | Fees and exchange rate | 10% | `fees_fx` | Yes |
| 5 | Card payment declined | 9% | `card_declined` | Often: error code, balance, limits |
| 6 | Limits: what they are, how to raise them | 7% | `limits` | Yes for information; increase request → human |
| 7 | Account restricted or under review | 7% | `account_restricted` | **Never** — always a human |
| 8 | Login, password, 2FA, suspicious activity | 7% | `login_security` | Information yes; suspicious activity → human |
| 9 | Dispute a card transaction, chargeback, refund | 5% | `dispute_chargeback` | Information yes; opening a dispute → human approves |
| 10 | Card lost, stolen or not delivered | 4% | `card_lost_stolen` | First instruction at once (freeze the card), then human |
| 11 | Change details, statements, close account | 4% | `account_management` | Often |
| 12 | Everything else | 2% | `other` | Human |

## 10. Demo customers

`data/seed/` contains 40 invented customers with accounts, cards and transactions. Names, emails and numbers are synthetic. Card numbers in seed data and test tickets use only public test ranges (for example 4111 1111 1111 1111) and must still pass through masking.
