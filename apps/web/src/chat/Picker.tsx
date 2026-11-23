import { useMutation } from "@tanstack/react-query";
import { type SubmitEvent, useState } from "react";
import { api, unwrap } from "../api/client";
import { ApiError } from "../api/problem";
import type { DemoCustomer, Profile } from "./profile";

function codeMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "invalid_access_code") return "This access code is not valid.";
    if (error.status === 429) return "Too many wrong codes. Please wait 15 minutes and try again.";
    if (error.status === 503) return "The demo chat is not available right now.";
  }
  return "Something went wrong. Please try again.";
}

/** Access code, then the demo customers as cards (UI §2.1). Replaces the app login. */
export function Picker({ onChosen }: { onChosen: (profile: Profile, customer: DemoCustomer) => void }) {
  const [code, setCode] = useState("");
  const customers = useMutation({
    mutationFn: async (accessCode: string) =>
      unwrap(await api.GET("/api/v1/demo/customers", { params: { header: { "X-Demo-Access-Code": accessCode } } })),
  });
  const login = useMutation({
    mutationFn: async (customer: DemoCustomer) => {
      const profile = unwrap(
        await api.POST("/api/v1/demo/session", { body: { customer_ref: customer.customer_ref, access_code: code } }),
      );
      return { profile, customer };
    },
    onSuccess: ({ profile, customer }) => {
      onChosen(profile, customer);
    },
  });

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    customers.mutate(code);
  }

  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      <h1 className="text-2xl font-semibold">Try the support chat</h1>
      <p className="mt-2 text-muted">
        In the real Tallybridge app you would already be logged in. Here you pick a demo customer instead — every
        customer and transaction is made up.
      </p>
      <form onSubmit={submit} className="mt-6 flex flex-wrap items-end gap-3" noValidate>
        <div>
          <label htmlFor="access-code" className="block text-sm font-medium">
            Access code
          </label>
          <input
            id="access-code"
            type="password"
            autoComplete="off"
            maxLength={200}
            value={code}
            onChange={(event) => {
              setCode(event.target.value);
            }}
            className="mt-1 w-64 max-w-full rounded-md border border-slate-300 px-3 py-2"
          />
        </div>
        <button
          type="submit"
          disabled={code === "" || customers.isPending}
          className="rounded-md bg-brand-600 px-4 py-2 font-medium text-white hover:bg-brand-700 disabled:opacity-60"
        >
          {customers.isPending ? "Checking…" : "Continue"}
        </button>
      </form>
      {customers.isError && (
        <p role="alert" className="mt-3 rounded-md bg-red-50 p-3 text-sm text-red-900">
          {codeMessage(customers.error)}
        </p>
      )}
      {customers.data && (
        <section aria-labelledby="pick-heading" className="mt-8">
          <h2 id="pick-heading" className="text-lg font-semibold">
            Choose a customer
          </h2>
          <ul className="mt-3 grid gap-3 sm:grid-cols-2">
            {customers.data.map((customer) => (
              <li key={customer.customer_ref}>
                <button
                  type="button"
                  disabled={login.isPending}
                  onClick={() => {
                    login.mutate(customer);
                  }}
                  className="w-full rounded-lg border border-slate-200 bg-white p-4 text-left hover:border-brand-600 hover:shadow-sm"
                >
                  <span className="block font-medium">{customer.display_name}</span>
                  <span className="block text-sm text-muted capitalize">{customer.tier}</span>
                  <span className="mt-1 block text-sm">{customer.hint}</span>
                </button>
              </li>
            ))}
          </ul>
          {login.isError && (
            <p role="alert" className="mt-3 rounded-md bg-red-50 p-3 text-sm text-red-900">
              {codeMessage(login.error)}
            </p>
          )}
        </section>
      )}
    </div>
  );
}
