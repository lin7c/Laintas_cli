// Its own module because both the header and the footer need it, and the
// footer is imported by the page that also defines the mark — leaving it in
// DownloadSection made that import cycle back on itself.
export default function BrandMark({ compact = false }) {
  return (
    <span className={`laintas-mark ${compact ? 'compact' : ''}`} aria-label="Laintas CLI">
      <b>L</b><i>&gt;</i>
    </span>
  );
}
