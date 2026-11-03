import { useEffect, useRef, useState } from 'react';
import { api } from './api';
export function useInsight<T>(path: string | null, revision = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [wake, setWake] = useState(0);
  const previous = useRef<string | null>(null);
  useEffect(() => {
    const visible = () => {
      if (document.visibilityState === 'visible') setWake((v) => v + 1);
    };
    document.addEventListener('visibilitychange', visible);
    return () => document.removeEventListener('visibilitychange', visible);
  }, []);
  useEffect(() => {
    if (!path) {
      setData(null);
      return;
    }
    const controller = new AbortController();
    const hide = () => {
      if (document.visibilityState === 'hidden') controller.abort();
    };
    if (document.visibilityState === 'hidden') return;
    document.addEventListener('visibilitychange', hide);
    if (previous.current !== path) setData(null);
    previous.current = path;
    setLoading(true);
    setError('');
    api<T>(path, { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setData(value);
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError((err as Error).message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => {
      controller.abort();
      document.removeEventListener('visibilitychange', hide);
    };
  }, [path, revision, wake]);
  return { data, error, loading };
}
