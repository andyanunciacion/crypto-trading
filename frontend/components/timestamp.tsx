import { formatManila, formatUtc } from "@/lib/format";

/** A time in Manila, with UTC on hover (UX7); `year` for dates that can span years. */
export function Timestamp({ iso, year = false }: { iso: string; year?: boolean }) {
  return (
    <time dateTime={iso} title={formatUtc(iso)} className="tabular-nums">
      {formatManila(iso, year)}
    </time>
  );
}
