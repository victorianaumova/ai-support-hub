/** Plain explanations of route reasons for the operator (UI §3 "Why you see this ticket";
 * codes: ai-pipeline §6). Unknown codes show as they are. */
export const REASONS: Record<string, string> = {
  policy_intent: "Policy: this kind of request always needs a human",
  fraud_report: "The customer reports fraud or unauthorised activity",
  injection: "The message tries to instruct the AI",
  complaint: "Complaint or legal matter",
  customer_request: "The customer asked for a human",
  action_request: "The customer wants something done, not only an answer",
  restricted_account: "The account is restricted",
  language: "Not written in English",
  budget_exceeded: "The daily AI budget was reached",
  system_error: "The AI run failed",
  masking_failed: "Card data could not be masked safely",
  no_kb_answer: "The help centre has no answer",
  policy_violation: "The draft broke an output rule",
  unsupported_claims: "The draft has claims the sources do not support",
  degraded: "Search was not available",
  policy_review: "Policy: drafts of this kind are reviewed",
  auto_reply_off: "Auto-reply is switched off",
  low_confidence: "Low confidence",
  operator_takeover: "An operator took over during the run",
};

/** The `approval_only` rule that sent a draft to approval (`review.rules`, ai-pipeline §6;
 * P12): shown under "Why you see this ticket" with the `policy_review` reason. */
export const REVIEW_RULES: Record<string, string> = {
  intent: "This topic is always reviewed",
  action: "The customer asks for a dispute",
  high_urgency: "High urgency",
  has_proposed_actions: "The draft proposes an action",
  memory_shows_open_case: "Repeat contact: the customer wrote about this before",
};

export function reviewRuleText(rule: string): string {
  return REVIEW_RULES[rule] ?? rule;
}

export function reasonText(code: string, confidence?: string | number | null): string {
  const text = REASONS[code] ?? code;
  if (code === "low_confidence" && confidence !== null && confidence !== undefined) {
    return `${text}: ${Number(confidence).toFixed(2)}`;
  }
  return text;
}

export const INTENTS = [
  "payment_pending",
  "withdrawal_issue",
  "card_declined",
  "card_lost_stolen",
  "kyc_verification",
  "account_restricted",
  "fees_fx",
  "limits",
  "dispute_chargeback",
  "login_security",
  "account_management",
  "other",
] as const;

export function words(code: string | null | undefined): string {
  return code ? code.replaceAll("_", " ") : "—";
}
