import { useEffect } from "react";
import { ChevronLeft, ChevronRight, Star, X } from "lucide-react";
import { mediaUrl } from "../lib/api";
import type { Take } from "../lib/types";

export function Lightbox({
  takes,
  index,
  pickedId,
  onClose,
  onNavigate,
  onPick,
}: {
  takes: Take[];
  index: number;
  pickedId?: number | null;
  onClose: () => void;
  onNavigate: (i: number) => void;
  onPick?: (take: Take) => void;
}) {
  const take = takes[index];

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowLeft" && index > 0) onNavigate(index - 1);
      if (e.key === "ArrowRight" && index < takes.length - 1) onNavigate(index + 1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [index, takes.length, onClose, onNavigate]);

  if (!take) return null;
  const url = mediaUrl(take.file_path ?? take.thumb_path);
  const isPicked = pickedId != null && take.id === pickedId;

  return (
    <div
      className="sb-fade-in fixed inset-0 z-[80] flex flex-col bg-ink-950/95 backdrop-blur-sm"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="flex items-center justify-between px-5 py-3">
        <span className="text-sm text-fog">
          Take {index + 1} of {takes.length}
          {take.seed != null && <span className="ml-3 text-fog/60">seed {take.seed}</span>}
        </span>
        <div className="flex items-center gap-2">
          {onPick && take.status === "done" && (
            <button
              onClick={() => onPick(take)}
              className={`flex h-8 items-center gap-1.5 rounded-md border px-3 text-sm font-medium transition-colors ${
                isPicked
                  ? "border-amber-450/60 bg-amber-450/15 text-amber-450"
                  : "border-line-bright text-mist hover:text-paper"
              }`}
            >
              <Star size={14} className={isPicked ? "fill-amber-450" : ""} />
              {isPicked ? "Picked" : "Pick this take"}
            </button>
          )}
          <button
            onClick={onClose}
            className="rounded-md p-1.5 text-fog hover:text-paper"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>
      </div>
      <div className="relative flex flex-1 items-center justify-center px-14 pb-8">
        {index > 0 && (
          <button
            onClick={() => onNavigate(index - 1)}
            className="absolute left-3 rounded-full border border-line-bright bg-ink-850/80 p-2 text-mist hover:text-paper"
            aria-label="Previous take"
          >
            <ChevronLeft size={18} />
          </button>
        )}
        {take.kind === "video" && url ? (
          <video src={url} controls autoPlay className="max-h-full max-w-full rounded-lg" />
        ) : url ? (
          <img src={url} alt="" className="max-h-full max-w-full rounded-lg object-contain" />
        ) : (
          <p className="text-sm text-fog">
            {take.status === "failed" ? (take.error ?? "This take failed.") : "Still rendering…"}
          </p>
        )}
        {index < takes.length - 1 && (
          <button
            onClick={() => onNavigate(index + 1)}
            className="absolute right-3 rounded-full border border-line-bright bg-ink-850/80 p-2 text-mist hover:text-paper"
            aria-label="Next take"
          >
            <ChevronRight size={18} />
          </button>
        )}
      </div>
    </div>
  );
}
