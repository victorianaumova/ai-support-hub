import { Link } from "react-router";
import { Icon } from "../components/Icon";
import type { IconName } from "../components/icons.generated";

const FACTS: { icon: IconName; title: string; text: string }[] = [
  { icon: "menu_book", title: "Cited answers", text: "Every fact links to the help article behind it." },
  { icon: "support_agent", title: "A human for risky cases", text: "Fraud, disputes and restricted accounts go to an operator." },
  { icon: "verified_user", title: "Every action recorded", text: "Card numbers are masked; the audit log is a verified chain." },
];

export function Landing() {
  return (
    <div className="mx-auto max-w-5xl px-4 py-8 sm:px-8 md:py-12">
      <section className="rounded-panel bg-[linear-gradient(110deg,#f8dce7,#e7daf2_50%,#dce0f8)] p-8 sm:p-12">
        <h1 className="max-w-2xl text-[2rem] sm:text-[2.5rem] leading-[1.1] font-medium tracking-[-0.025em]">AI customer support, with a human in charge</h1>
        <p className="mt-4 max-w-2xl text-base text-[var(--tb-text-2)]">
          Tallybridge is a fictional payments company. Its support assistant answers routine questions with
          sources from the help centre and sends anything risky — fraud, disputes, restricted accounts — to a
          human operator. All data here is synthetic.
        </p>
        <div className="mt-8 flex flex-wrap gap-3">
          <Link to="/chat" className="btn btn-primary">
            <Icon name="chat" size={18} />
            Try as a customer
          </Link>
          <Link to="/console" className="btn btn-secondary">
            Open operator console
            <Icon name="arrow_forward" size={18} />
          </Link>
        </div>
      </section>
      <ul className="mt-2 grid gap-2 sm:grid-cols-3">
        {FACTS.map((fact) => (
          <li key={fact.title} className="panel flex items-start gap-4 p-5">
            <span aria-hidden="true" className="flex size-10 shrink-0 items-center justify-center rounded-full bg-surface-hover text-ink">
              <Icon name={fact.icon} />
            </span>
            <span>
              <span className="block font-medium">{fact.title}</span>
              <span className="mt-0.5 block text-muted">{fact.text}</span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
