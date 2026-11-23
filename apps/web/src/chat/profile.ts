import type { components } from "../api/schema";

export type Profile = components["schemas"]["CustomerProfileOut"];
export type DemoCustomer = components["schemas"]["DemoCustomerOut"];

const KEY = "hub.chat.profile";

/** Name, tier and reference of the chosen demo customer, for the chat header after a reload.
 * Per tab only; the session itself is the HttpOnly cookie. Storage may be blocked: then the
 * customer simply picks again. */
export function loadProfile(): Profile | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    return raw === null ? null : (JSON.parse(raw) as Profile);
  } catch {
    return null;
  }
}

export function saveProfile(profile: Profile | null): void {
  try {
    if (profile === null) sessionStorage.removeItem(KEY);
    else sessionStorage.setItem(KEY, JSON.stringify(profile));
  } catch {
    // storage blocked: nothing to remember
  }
}
