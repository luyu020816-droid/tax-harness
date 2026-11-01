import { useEffect } from 'react';

/** Decorative feedback stays outside the chat's request and state lifecycle. */
export function OrbitEffects() {
  useEffect(() => {
    const root = document.getElementById('root');
    if (!root) return;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    const fine = window.matchMedia('(hover: hover) and (pointer: fine)');
    const controller = new AbortController();
    const waves = new Set<HTMLElement>();
    let illuminated: HTMLElement | null = null;
    let frame = 0;
    const reset = () => {
      cancelAnimationFrame(frame);
      if (!illuminated) return;
      delete illuminated.dataset.orbitLit;
      for (const name of ['--pointer-x', '--pointer-y', '--tilt-x', '--tilt-y'])
        illuminated.style.removeProperty(name);
      illuminated = null;
    };
    const move = (event: PointerEvent) => {
      if (reduced.matches || !fine.matches || event.pointerType === 'touch') return;
      const target =
        event.target instanceof Element
          ? event.target.closest<HTMLElement>(
              '.orbit-starters button, .orbit-rule-list button, .composer, .orbit-mini-scene'
            )
          : null;
      if (target !== illuminated) reset();
      if (!target) return;
      illuminated = target;
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        if (!target.isConnected) return;
        const bounds = target.getBoundingClientRect();
        const x = Math.max(0, Math.min(bounds.width, event.clientX - bounds.left));
        const y = Math.max(0, Math.min(bounds.height, event.clientY - bounds.top));
        target.style.setProperty('--pointer-x', `${x}px`);
        target.style.setProperty('--pointer-y', `${y}px`);
        target.style.setProperty('--tilt-x', `${(0.5 - y / bounds.height) * 5}deg`);
        target.style.setProperty('--tilt-y', `${(x / bounds.width - 0.5) * 5}deg`);
        target.dataset.orbitLit = 'true';
      });
    };
    const ripple = (event: PointerEvent | MouseEvent) => {
      if (reduced.matches || (event instanceof PointerEvent && event.button !== 0)) return;
      const button = event.target instanceof Element ? event.target.closest('button') : null;
      if (!button || button.disabled) return;
      const rect = button.getBoundingClientRect();
      const size = Math.min(160, Math.max(64, rect.width));
      const keyboard = event.type === 'click';
      const wave = document.createElement('span');
      wave.className = 'orbit-click-wave';
      wave.setAttribute('aria-hidden', 'true');
      Object.assign(wave.style, {
        width: `${size}px`,
        height: `${size}px`,
        left: `${(keyboard ? rect.left + rect.width / 2 : event.clientX) - size / 2}px`,
        top: `${(keyboard ? rect.top + rect.height / 2 : event.clientY) - size / 2}px`,
      });
      document.body.appendChild(wave);
      waves.add(wave);
      const animation = wave.animate(
        [
          { transform: 'scale(.12)', opacity: 0.8 },
          { transform: 'scale(1)', opacity: 0 },
        ],
        { duration: 560, easing: 'cubic-bezier(.16,1,.3,1)' }
      );
      const remove = () => {
        wave.remove();
        waves.delete(wave);
      };
      void animation.finished.then(remove, remove);
    };
    const preferencesChanged = () => {
      reset();
      for (const wave of waves) {
        wave.getAnimations().forEach((a) => a.cancel());
        wave.remove();
      }
      waves.clear();
    };
    root.addEventListener('pointermove', move, { passive: true, signal: controller.signal });
    root.addEventListener('pointerleave', reset, { signal: controller.signal });
    root.addEventListener('pointerdown', ripple, { passive: true, signal: controller.signal });
    root.addEventListener(
      'click',
      (event) => {
        if (event.detail === 0) ripple(event);
      },
      { signal: controller.signal }
    );
    reduced.addEventListener('change', preferencesChanged);
    fine.addEventListener('change', preferencesChanged);
    return () => {
      controller.abort();
      preferencesChanged();
      reduced.removeEventListener('change', preferencesChanged);
      fine.removeEventListener('change', preferencesChanged);
    };
  }, []);
  return null;
}
