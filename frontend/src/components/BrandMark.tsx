/** The StoryBored mark: a 2×2 storyboard grid, three panels empty ("bored"),
 *  one lit amber with a play cut-out. Inline copy of assets/brand — keep in
 *  sync with assets/brand/THEME.md. */
export function BrandMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" role="img" aria-label="StoryBored" className={className}>
      <rect x="1" y="1" width="62" height="62" rx="13.5" fill="#0a0a0c" stroke="#34343d" strokeWidth="2" />
      <rect x="35" y="9" width="20" height="20" rx="5" fill="none" stroke="#8b8a94" strokeOpacity="0.5" strokeWidth="3.5" />
      <rect x="9" y="35" width="20" height="20" rx="5" fill="none" stroke="#8b8a94" strokeOpacity="0.5" strokeWidth="3.5" />
      <rect x="35" y="35" width="20" height="20" rx="5" fill="none" stroke="#8b8a94" strokeOpacity="0.5" strokeWidth="3.5" />
      <rect x="9" y="9" width="20" height="20" rx="5" fill="#f0b429" />
      <path
        d="M16.8 14.9 L23.6 19 L16.8 23.1 Z"
        fill="#0a0a0c"
        stroke="#0a0a0c"
        strokeWidth="2"
        strokeLinejoin="round"
      />
    </svg>
  );
}
