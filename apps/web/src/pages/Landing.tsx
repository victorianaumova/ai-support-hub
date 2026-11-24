import { Link } from "react-router";

export function Landing() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-12">
      <h1 className="text-3xl font-normal tracking-tight">AI customer support, with a human in charge</h1>
      <p className="mt-4 text-base text-muted">
        Tallybridge is a fictional payments company. Its support assistant answers routine questions with
        sources from the help centre and sends anything risky — fraud, disputes, restricted accounts — to a
        human operator. All data here is synthetic.
      </p>
      <div className="mt-8 flex flex-wrap gap-3">
        <Link to="/chat" className="btn btn-primary">
          Try as a customer
        </Link>
        <Link to="/console" className="btn btn-secondary">
          Open operator console
        </Link>
      </div>
    </div>
  );
}
