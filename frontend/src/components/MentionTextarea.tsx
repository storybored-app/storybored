import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type TextareaHTMLAttributes,
} from "react";
import { useQuery } from "@tanstack/react-query";
import { AtSign } from "lucide-react";
import { apiGet, mediaUrl } from "../lib/api";
import type { Character } from "../lib/types";

interface Props
  extends Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "value" | "onChange"> {
  value: string;
  onChange: (v: string) => void;
}

/** Find an "@partial" token immediately before the caret. */
function mentionAt(value: string, caret: number): { start: number; query: string } | null {
  const upto = value.slice(0, caret);
  const m = /(^|[\s(])@([a-zA-Z0-9_-]*)$/.exec(upto);
  if (!m) return null;
  return { start: caret - m[2].length - 1, query: m[2].toLowerCase() };
}

export function MentionTextarea({ value, onChange, className = "", ...rest }: Props) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const [mention, setMention] = useState<{ start: number; query: string } | null>(null);
  const [highlight, setHighlight] = useState(0);

  const { data: characters } = useQuery<Character[]>({
    queryKey: ["characters"],
    queryFn: () => apiGet<Character[]>("/api/characters"),
    staleTime: 30_000,
    retry: 1,
  });

  const matches = useMemo(() => {
    if (!mention || !characters) return [];
    return characters
      .filter(
        (c) =>
          c.handle.toLowerCase().startsWith(mention.query) ||
          c.name.toLowerCase().startsWith(mention.query),
      )
      .slice(0, 6);
  }, [mention, characters]);

  useEffect(() => setHighlight(0), [mention?.query]);

  const refreshMention = () => {
    const el = ref.current;
    if (!el) return;
    setMention(mentionAt(el.value, el.selectionStart ?? el.value.length));
  };

  const insert = (c: Character) => {
    const el = ref.current;
    if (!el || !mention) return;
    const caret = el.selectionStart ?? value.length;
    const next = `${value.slice(0, mention.start)}@${c.handle} ${value.slice(caret)}`;
    onChange(next);
    setMention(null);
    requestAnimationFrame(() => {
      el.focus();
      const pos = mention.start + c.handle.length + 2;
      el.setSelectionRange(pos, pos);
    });
  };

  return (
    <div className="relative">
      <textarea
        ref={ref}
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          requestAnimationFrame(refreshMention);
        }}
        onKeyDown={(e) => {
          if (!mention || matches.length === 0) return;
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setHighlight((h) => (h + 1) % matches.length);
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setHighlight((h) => (h - 1 + matches.length) % matches.length);
          } else if (e.key === "Enter" || e.key === "Tab") {
            e.preventDefault();
            insert(matches[highlight]);
          } else if (e.key === "Escape") {
            setMention(null);
          }
        }}
        onClick={refreshMention}
        onBlur={() => window.setTimeout(() => setMention(null), 150)}
        className={`w-full rounded-md border border-line bg-ink-900 px-3 py-2 text-sm leading-relaxed text-paper placeholder:text-fog/60 transition-colors focus:border-amber-450/50 focus:outline-none ${className}`}
        {...rest}
      />
      {mention && matches.length > 0 && (
        <div className="sb-fade-in absolute left-0 right-0 top-full z-30 mt-1 overflow-hidden rounded-lg border border-line-bright bg-ink-850 shadow-2xl">
          {matches.map((c, i) => (
            <button
              key={c.id}
              type="button"
              onMouseDown={(e) => {
                e.preventDefault();
                insert(c);
              }}
              onMouseEnter={() => setHighlight(i)}
              className={`flex w-full items-center gap-2.5 px-3 py-2 text-left ${
                i === highlight ? "bg-ink-700/70" : ""
              }`}
            >
              {c.thumbnail_path ? (
                <img
                  src={mediaUrl(c.thumbnail_path)}
                  alt=""
                  className="h-7 w-7 rounded-full border border-line object-cover"
                />
              ) : (
                <span className="flex h-7 w-7 items-center justify-center rounded-full border border-line bg-ink-800">
                  <AtSign size={12} className="text-fog" />
                </span>
              )}
              <span className="min-w-0">
                <span className="block truncate text-sm text-paper">{c.name}</span>
                <span className="block truncate text-xs text-fog">@{c.handle}</span>
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
