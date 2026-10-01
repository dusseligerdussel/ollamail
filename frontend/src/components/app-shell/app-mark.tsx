/** The ollamail logo mark (same shape as the favicon). */
export function AppMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" className={className}>
      <rect width="16" height="16" rx="4" className="fill-foreground" />
      <path
        d="M3.75 5.25h8.5v5.5h-8.5zM3.75 5.25 8 8.5l4.25-3.25"
        fill="none"
        strokeWidth="1.2"
        strokeLinejoin="round"
        className="stroke-background"
      />
    </svg>
  );
}
