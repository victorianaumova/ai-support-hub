import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { useLocation, useParams } from "react-router";
import { api, unwrap } from "../api/client";
import { ApiError } from "../api/problem";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { SafeText } from "../text/SafeText";

/** A public help article (`external` KB document), the target of citation links (UI §1). */
export function HelpArticle() {
  const { docId = "" } = useParams();
  const { hash } = useLocation();
  const article = useQuery({
    queryKey: ["help", docId],
    queryFn: async () => unwrap(await api.GET("/api/v1/help/{doc_id}", { params: { path: { doc_id: docId } } })),
  });

  useEffect(() => {
    if (!article.data || hash === "") return;
    const target = document.getElementById(decodeURIComponent(hash.slice(1)));
    target?.scrollIntoView();
    target?.focus();
  }, [article.data, hash]);

  if (article.isPending) return <Loading label="Loading the article" />;
  if (article.isError) {
    if (article.error instanceof ApiError && article.error.status === 404) {
      return (
        <div className="mx-auto max-w-3xl px-4 py-8">
          <h1 className="text-2xl font-semibold">Article not found</h1>
        </div>
      );
    }
    return <ErrorState error={article.error} onRetry={() => void article.refetch()} />;
  }
  const doc = article.data;
  return (
    <article className="panel mx-4 my-8 max-w-3xl px-6 py-8 sm:mx-auto sm:px-8">
      <p className="text-sm text-muted">Tallybridge help centre</p>
      <h1 className="mt-1 text-3xl font-normal">{doc.title}</h1>
      <p className="mt-1 text-sm text-muted">Updated {new Date(doc.updated_at).toLocaleDateString()}</p>
      {doc.sections.map((section) => (
        <section key={section.anchor} id={section.anchor} tabIndex={-1} className="mt-6 scroll-mt-4 rounded focus:bg-amber-50">
          {section.heading !== "" && <h2 className="text-xl font-semibold">{section.heading}</h2>}
          <div className="mt-2">
            <SafeText text={section.text} />
          </div>
        </section>
      ))}
    </article>
  );
}
