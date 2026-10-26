import { useEffect, useId, useRef, type ReactNode } from 'react';
import { X } from 'lucide-react';

export function Modal({
  title,
  children,
  onClose,
  drawer = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  drawer?: boolean;
}) {
  const container = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const element = container.current;
    const first = element?.querySelector<HTMLElement>(
      '[data-autofocus],input,textarea,select,button'
    );
    first?.focus();
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeRef.current();
      if (event.key !== 'Tab' || !element) return;
      const items = Array.from(
        element.querySelectorAll<HTMLElement>(
          'button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),a[href],[tabindex="0"]'
        )
      );
      const firstItem = items[0],
        lastItem = items[items.length - 1];
      if (event.shiftKey && document.activeElement === firstItem) {
        event.preventDefault();
        lastItem?.focus();
      } else if (!event.shiftKey && document.activeElement === lastItem) {
        event.preventDefault();
        firstItem?.focus();
      }
    };
    document.addEventListener('keydown', handler);
    const oldOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', handler);
      document.body.style.overflow = oldOverflow;
      previous?.focus();
    };
  }, []);
  return (
    <div
      className={`overlay ${drawer ? 'overlay-drawer' : ''}`}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={container}
        className={drawer ? 'drawer' : 'modal'}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <header className="dialog-heading">
          <h2 id={titleId}>{title}</h2>
          <button className="icon-button" onClick={onClose} aria-label="关闭">
            <X size={20} />
          </button>
        </header>
        <div className="dialog-body">{children}</div>
      </div>
    </div>
  );
}
