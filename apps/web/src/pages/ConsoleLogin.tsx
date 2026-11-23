import { type SubmitEvent, useState } from "react";
import { Navigate, useSearchParams } from "react-router";
import { ApiError } from "../api/problem";
import { useLogin, useOperator } from "../auth/session";

/** Only a path inside the console may be a target after login (no open redirect). */
export function safeNext(value: string | null): string {
  if (value === null || !value.startsWith("/console") || value.startsWith("//")) return "/console";
  return value;
}

function loginMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "Email or password is not correct.";
    if (error.status === 429) return "Too many attempts. Please wait 15 minutes and try again.";
    if (error.status === 503) return "Login is not available right now.";
  }
  return "Login failed. Please try again.";
}

export function ConsoleLogin() {
  const [params] = useSearchParams();
  const session = useOperator();
  const login = useLogin();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const next = safeNext(params.get("next"));

  if (session.data) return <Navigate to={next} replace />;

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    login.mutate({ email, password });
  }

  return (
    <div className="mx-auto max-w-sm px-4 py-12">
      <h1 className="text-2xl font-semibold">Operator login</h1>
      <form onSubmit={submit} className="mt-6 space-y-4" noValidate>
        <div>
          <label htmlFor="email" className="block text-sm font-medium">
            Email
          </label>
          <input
            id="email"
            type="email"
            autoComplete="username"
            required
            maxLength={254}
            value={email}
            onChange={(event) => {
              setEmail(event.target.value);
            }}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
          />
        </div>
        <div>
          <label htmlFor="password" className="block text-sm font-medium">
            Password
          </label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            required
            maxLength={1024}
            value={password}
            onChange={(event) => {
              setPassword(event.target.value);
            }}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
          />
        </div>
        {login.isError && (
          <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-900">
            {loginMessage(login.error)}
          </p>
        )}
        <button
          type="submit"
          disabled={login.isPending || email === "" || password === ""}
          className="w-full rounded-md bg-brand-600 px-4 py-2.5 font-medium text-white hover:bg-brand-700 disabled:opacity-60"
        >
          {login.isPending ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
