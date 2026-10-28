"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiError } from "@/lib/api";
import { PAGE_ROWS } from "@/lib/history";

type Pages<T> = {
  key: string; // the query these rows are for
  rows: T[];
  more: boolean; // a full page came back: there may be older rows
  error: string | null;
};

export function problemText(error: unknown): string {
  return error instanceof ApiError ? error.message : "Couldn't reach the API.";
}

/**
 * Rows of a newest-first list paged by id (History, risk events): the first page for
 * `query`, read again whenever it changes, and `older()` to add the page after the
 * last row. `pages` is null while the first page for the current query loads, so rows
 * from an older query are never shown as this one's.
 */
export function usePages<T extends { id: number }>(
  fetchPage: (query: URLSearchParams, before?: number) => Promise<T[]>,
  query: string,
) {
  const [pages, setPages] = useState<Pages<T> | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);

  useEffect(() => {
    let current = true;
    fetchPage(new URLSearchParams(query))
      .then((rows) => {
        if (current) setPages({ key: query, rows, more: rows.length === PAGE_ROWS, error: null });
      })
      .catch((error: unknown) => {
        if (current) setPages({ key: query, rows: [], more: false, error: problemText(error) });
      });
    return () => {
      current = false;
    };
  }, [fetchPage, query]);

  const shown = pages?.key === query ? pages : null;
  const last = shown?.rows.at(-1)?.id;

  const older = useCallback(() => {
    if (last === undefined) return;
    setLoadingOlder(true);
    fetchPage(new URLSearchParams(query), last)
      .then((rows) =>
        setPages((before) =>
          before?.key === query
            ? { ...before, rows: [...before.rows, ...rows], more: rows.length === PAGE_ROWS }
            : before,
        ),
      )
      .catch((error: unknown) =>
        setPages((before) =>
          before?.key === query ? { ...before, error: problemText(error) } : before,
        ),
      )
      .finally(() => setLoadingOlder(false));
  }, [fetchPage, query, last]);

  return { pages: shown, older, loadingOlder };
}
