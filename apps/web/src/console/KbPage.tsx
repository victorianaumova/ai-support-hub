import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, unwrap } from "../api/client";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { SafeText } from "../text/SafeText";

function Badge({ visibility }: { visibility: string }) {
  const internal = visibility === "internal";
  return (
    <span className={`chip ${internal ? "chip-warn" : "chip-ok"}`}>
      {internal ? "Internal — never send to customers" : "External"}
    </span>
  );
}

/** UI §3: search over every chunk (internal included), results with a badge, article view. */
export function KbPage() {
  const [input, setInput] = useState("");
  const [q, setQ] = useState("");
  const [mode, setMode] = useState<"hybrid" | "dense">("hybrid");
  const [docId, setDocId] = useState<string | null>(null);
  const results = useQuery({
    queryKey: ["console", "kb", q, mode],
    enabled: q !== "",
    queryFn: async () => unwrap(await api.GET("/api/v1/console/kb/search", { params: { query: { q, mode, limit: 10 } } })).items,
  });
  const article = useQuery({
    queryKey: ["console", "kb-doc", docId],
    enabled: docId !== null,
    queryFn: async () => unwrap(await api.GET("/api/v1/console/kb/documents/{doc_id}", { params: { path: { doc_id: docId ?? "" } } })),
  });

  return (
    <div className="px-4 py-4 sm:px-6">
      <h1 className="flex h-12 items-center text-[1.375rem] font-normal">Knowledge base</h1>
      <form
        role="search"
        className="mt-4 flex flex-wrap gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          setQ(input.trim());
          setDocId(null);
        }}
      >
        <label htmlFor="kb-q" className="sr-only">
          Search the knowledge base
        </label>
        <input id="kb-q" value={input} maxLength={500} onChange={(e) => { setInput(e.target.value); }} placeholder="e.g. SEPA withdrawal time" className="min-w-0 flex-1 field" />
        <label className="flex items-center gap-1 text-sm">
          Mode
          <select value={mode} onChange={(e) => { setMode(e.target.value as "hybrid" | "dense"); }} className="field">
            <option value="hybrid">hybrid</option>
            <option value="dense">dense</option>
          </select>
        </label>
        <button type="submit" disabled={input.trim() === ""} className="btn btn-primary">
          Search
        </button>
      </form>
      <div className="mt-6 grid gap-4 lg:grid-cols-2">
        <section aria-label="Results">
          {results.isFetching && <Loading label="Searching" />}
          {results.isError && <ErrorState error={results.error} onRetry={() => void results.refetch()} />}
          {results.data?.length === 0 && <p className="text-muted">No results.</p>}
          <ol className="space-y-3">
            {results.data?.map((hit) => (
              <li key={hit.chunk_id} className="panel p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <button type="button" onClick={() => { setDocId(hit.doc_id); }} className="font-medium text-primary-text hover:underline">
                    {hit.title}
                  </button>
                  <Badge visibility={hit.visibility} />
                </div>
                <p className="text-xs text-muted">
                  {hit.section} · score {hit.score.toFixed(3)} · rank {hit.rank}
                </p>
                <div className="mt-1 line-clamp-4">
                  <SafeText text={hit.text} />
                </div>
              </li>
            ))}
          </ol>
        </section>
        <section aria-label="Article" className="panel p-4">
          {docId === null && <p className="text-muted">Choose a result to read the article.</p>}
          {article.isPending && docId !== null && <Loading label="Loading the article" />}
          {article.isError && <ErrorState error={article.error} />}
          {article.data && (
            <article>
              <h2 className="text-xl font-semibold">{article.data.title}</h2>
              <Badge visibility={article.data.visibility} />
              {article.data.sections.map((section) => (
                <section key={section.anchor} className="mt-4">
                  {section.heading !== "" && <h3 className="font-semibold">{section.heading}</h3>}
                  <SafeText text={section.text} />
                </section>
              ))}
            </article>
          )}
        </section>
      </div>
    </div>
  );
}
