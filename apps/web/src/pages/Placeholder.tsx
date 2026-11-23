/** A route that a later task fills (P10-02 to P10-04). */
export function Placeholder({ title, task }: { title: string; task: string }) {
  return (
    <div className="mx-auto max-w-5xl px-4 py-8">
      <h1 className="text-2xl font-semibold">{title}</h1>
      <p className="mt-2 text-muted">This page is built in {task}.</p>
    </div>
  );
}
