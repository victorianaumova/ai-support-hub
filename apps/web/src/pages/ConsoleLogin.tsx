import { type SubmitEvent, useState } from "react";
import { Navigate, useSearchParams } from "react-router";
import { ApiError } from "../api/problem";
import { useLogin, useOperator } from "../auth/session";
import { LogoMark } from "../components/Logo";

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
    <div className="flex justify-center px-4 py-12 sm:py-16">
      <div className="panel w-full max-w-sm p-8">
      <div className="flex flex-col items-center text-center">
        <LogoMark size={40} />
        <h1 className="mt-4 text-2xl font-normal">Operator login</h1>
        <p className="mt-1 text-muted">Sign in to the Tallybridge support console</p>
      </div>
      <form onSubmit={submit} className="mt-8 space-y-4" noValidate>
        <div>
          <label htmlFor="email" className="block font-medium">
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
            className="field mt-1 w-full px-3"
          />
        </div>
        <div>
          <label htmlFor="password" className="block font-medium">
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
            className="field mt-1 w-full px-3"
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
          className="btn btn-primary w-full"
        >
          {login.isPending ? "Signing in…" : "Sign in"}
        </button>
      </form>
      </div>
    </div>
  );
}
