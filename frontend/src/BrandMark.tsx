import brandMarkUrl from './assets/voucher-fox.svg';
import smallBrandMarkUrl from './assets/voucher-fox-small.svg';

/** Voucher fox identity; the compact variant preserves its silhouette at small sizes. */
export function BrandMark({
  className = '',
  compact = false,
}: {
  className?: string;
  compact?: boolean;
}) {
  return (
    <img
      className={`brand-mark ${className}`.trim()}
      src={compact ? smallBrandMarkUrl : brandMarkUrl}
      width={32}
      height={32}
      alt=""
      aria-hidden="true"
    />
  );
}
