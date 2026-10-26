import { formatManila, formatUtc } from "@/lib/format";

/** A time in Manila, with UTC on hover (UX7). */
export function Timestamp({ iso }: { iso: string }) {
  return (
    <time dateTime={iso} title={formatUtc(iso)} className="tabular-nums">
      {formatManila(iso)}
    </time>
  );
}
