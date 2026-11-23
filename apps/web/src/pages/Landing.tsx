import { Link } from "react-router";

export function Landing() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-12">
      <h1 className="text-3xl font-semibold tracking-tight">AI customer support, with a human in charge</h1>
      <p className="mt-4 text-lg text-muted">
        Tallybridge is a fictional payments company. Its support assistant answers routine questions with
        sources from the help centre and sends anything risky — fraud, disputes, restricted accounts — to a
        human operator. All data here is synthetic.
      </p>
      <div className="mt-8 flex flex-wrap gap-3">
        <Link to="/chat" className="rounded-md bg-brand-600 px-4 py-2.5 font-medium text-white hover:bg-brand-700">
          Try as a customer
        </Link>
        <Link to="/console" className="rounded-md border border-slate-300 bg-white px-4 py-2.5 font-medium hover:bg-slate-100">
          Open operator console
        </Link>
      </div>
    </div>
  );
}
