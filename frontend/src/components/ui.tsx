import {
  forwardRef,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { Loader2 } from "lucide-react";

type ButtonVariant = "primary" | "ghost" | "outline" | "danger";

const BTN_BASE =
  "inline-flex items-center justify-center gap-1.5 rounded-md text-sm font-medium transition-colors disabled:opacity-45 disabled:pointer-events-none focus-visible:outline-2 focus-visible:outline-amber-450/60 select-none";

const BTN_VARIANTS: Record<ButtonVariant, string> = {
  primary: "bg-amber-450 text-ink-950 hover:bg-amber-350 font-semibold",
  ghost: "text-mist hover:text-paper hover:bg-ink-700/60",
  outline: "border border-line-bright text-mist hover:text-paper hover:border-fog/60 bg-transparent",
  danger: "border border-status-failed/40 text-status-failed hover:bg-status-failed/10",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: "sm" | "md";
  busy?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "outline", size = "md", busy, className = "", children, disabled, ...rest },
  ref,
) {
  const sz = size === "sm" ? "h-7 px-2.5 text-xs" : "h-9 px-3.5";
  return (
    <button
      ref={ref}
      className={`${BTN_BASE} ${BTN_VARIANTS[variant]} ${sz} ${className}`}
      disabled={disabled || busy}
      {...rest}
    >
      {busy && <Loader2 size={14} className="animate-spin" />}
      {children}
    </button>
  );
});

const CONTROL =
  "w-full rounded-md border border-line bg-ink-900 px-3 text-sm text-paper placeholder:text-fog/60 focus:border-amber-450/50 focus:outline-none transition-colors";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className = "", ...rest }, ref) {
    return <input ref={ref} className={`${CONTROL} h-9 ${className}`} {...rest} />;
  },
);

export const TextArea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement>
>(function TextArea({ className = "", ...rest }, ref) {
  return (
    <textarea
      ref={ref}
      className={`${CONTROL} py-2 leading-relaxed ${className}`}
      {...rest}
    />
  );
});

export const Select = forwardRef<
  HTMLSelectElement,
  SelectHTMLAttributes<HTMLSelectElement>
>(function Select({ className = "", children, ...rest }, ref) {
  return (
    <select ref={ref} className={`${CONTROL} h-9 appearance-none pr-8 ${className}`} {...rest}>
      {children}
    </select>
  );
});

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-fog">
        {label}
      </span>
      {children}
      {hint && <span className="mt-1 block text-xs text-fog/80">{hint}</span>}
    </label>
  );
}

export function Badge({
  tone = "fog",
  children,
  pulse,
}: {
  tone?: "fog" | "amber" | "blue" | "green" | "red";
  children: ReactNode;
  pulse?: boolean;
}) {
  const tones: Record<string, string> = {
    fog: "border-line-bright text-fog",
    amber: "border-amber-450/40 text-amber-450",
    blue: "border-status-generated/40 text-status-generated",
    green: "border-status-approved/40 text-status-approved",
    red: "border-status-failed/40 text-status-failed",
  };
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium ${tones[tone]}`}
    >
      {pulse && (
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
      )}
      {children}
    </span>
  );
}

export function Spinner({ size = 16 }: { size?: number }) {
  return <Loader2 size={size} className="animate-spin text-fog" />;
}

export function ProgressBar({ value, tone = "amber" }: { value: number; tone?: "amber" | "green" }) {
  const pct = Math.max(0, Math.min(100, Math.round(value * 100)));
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-ink-700">
      <div
        className={`h-full rounded-full transition-[width] duration-500 ${
          tone === "green" ? "bg-status-approved" : "bg-amber-450"
        }`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}
