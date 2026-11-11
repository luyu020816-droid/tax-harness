import { useEffect, useRef, useState } from 'react';
import { Archive, History, MessageSquare, MoreHorizontal, Pin, Trash2 } from 'lucide-react';
import { api, body } from './api';
import { Modal } from './Modal';
import type { Session } from './types';

interface Props {
  sessions: Session[];
  activeId?: string;
  onOpen: (session: Session) => void;
  onChanged: (id: string, action: string) => Promise<void>;
}
export function SessionHistory({ sessions, activeId, onOpen, onChanged }: Props) {
  const [show, setShow] = useState(false);
  const [archived, setArchived] = useState(false);
  const [rows, setRows] = useState<Session[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState<Session | null>(null);
  const alive = useRef(true);
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    const closeOutside = (event: PointerEvent) => {
      document.querySelectorAll<HTMLDetailsElement>('.session-actions[open]').forEach((menu) => {
        if (!menu.contains(event.target as Node)) menu.open = false;
      });
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      document.querySelectorAll<HTMLDetailsElement>('.session-actions[open]').forEach((menu) => {
        menu.open = false;
        menu.querySelector('summary')?.focus();
      });
    };
    document.addEventListener('pointerdown', closeOutside);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('pointerdown', closeOutside);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, []);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      request.current?.abort();
    };
  }, []);
  async function load(isArchived: boolean, offset = 0) {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError('');
    try {
      const value = await api<{ sessions: Session[]; next_offset: number | null }>(
        `/sessions/history?archived=${isArchived}&offset=${offset}`,
        { signal: controller.signal }
      );
      if (!controller.signal.aborted && alive.current) {
        setRows((old) => (offset ? [...old, ...value.sessions] : value.sessions));
        setNext(value.next_offset);
      }
    } catch (e) {
      if (!controller.signal.aborted && alive.current) setError((e as Error).message);
    } finally {
      if (!controller.signal.aborted && alive.current) setBusy(false);
    }
  }
  async function act(session: Session, action: string) {
    setBusy(true);
    setError('');
    try {
      await api(`/sessions/${session.session_id}/organization`, {
        method: 'POST',
        body: body({ action, expected_revision: session.organization_revision ?? 1 }),
      });
      if (!alive.current) return;
      setDeleting(null);
      await onChanged(session.session_id, action);
      if (show && alive.current) await load(archived);
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  function row(session: Session) {
    return (
      <div className="session-history-row" key={session.session_id}>
        <button
          className={session.session_id === activeId ? 'active' : ''}
          title={session.title || '新对话'}
          aria-label={`打开会话：${session.title || '新对话'}`}
          onClick={() => {
            onOpen(session);
            setShow(false);
          }}
        >
          {session.pinned_at ? <Pin size={14} /> : <MessageSquare size={14} />}
          <span>{session.title || '新对话'}</span>
        </button>
        <details className="session-actions">
          <summary aria-label={`会话操作：${session.title || '新对话'}`}>
            <MoreHorizontal size={16} />
          </summary>
          <div
            className="session-actions-menu"
            onClick={(e) => e.currentTarget.closest('details')?.removeAttribute('open')}
          >
            <button
              disabled={busy}
              onClick={() => void act(session, session.pinned_at ? 'unpin' : 'pin')}
            >
              <Pin size={14} />
              {session.pinned_at ? '取消置顶' : '置顶'}
            </button>
            <button
              disabled={busy}
              onClick={() => void act(session, session.archived_at ? 'unarchive' : 'archive')}
            >
              <Archive size={14} />
              {session.archived_at ? '取消归档' : '归档'}
            </button>
            <button disabled={busy} onClick={() => setDeleting(session)}>
              <Trash2 size={14} />
              删除
            </button>
          </div>
        </details>
      </div>
    );
  }
  return (
    <>
      <button
        className="session-history-launcher"
        aria-label="管理历史会话"
        onClick={() => {
          setShow(true);
          setArchived(false);
          void load(false);
        }}
      >
        <History size={18} />
      </button>
      <div className="recent-section">
        <span className="nav-label">最近会话</span>
        {sessions.slice(0, 8).map(row)}
        <button
          className="history-all"
          onClick={() => {
            setShow(true);
            setArchived(false);
            void load(false);
          }}
        >
          全部会话与归档
        </button>
        {error && !show && (
          <p className="error-note" role="alert">
            {error}
          </p>
        )}
      </div>
      {show && !deleting && (
        <Modal
          title="历史会话"
          onClose={() => {
            setShow(false);
            request.current?.abort();
          }}
        >
          <div className="button-row">
            <button
              className={!archived ? 'primary' : 'secondary'}
              onClick={() => {
                setArchived(false);
                setRows([]);
                void load(false);
              }}
            >
              全部会话
            </button>
            <button
              className={archived ? 'primary' : 'secondary'}
              onClick={() => {
                setArchived(true);
                setRows([]);
                void load(true);
              }}
            >
              已归档
            </button>
          </div>
          <div className="history-full-list">{rows.map(row)}</div>
          {busy && <p className="muted">正在读取…</p>}
          {!busy && !rows.length && <p className="muted">暂无会话</p>}
          {error && (
            <p className="error-note" role="alert">
              {error}
            </p>
          )}
          {next !== null && (
            <button className="secondary" disabled={busy} onClick={() => void load(archived, next)}>
              加载更多会话
            </button>
          )}
        </Modal>
      )}
      {deleting && (
        <Modal
          title="删除会话"
          onClose={() => {
            if (!busy) setDeleting(null);
          }}
        >
          <p>
            删除“{deleting.title || '新对话'}
            ”？删除后不能继续此会话。已独立提交的知识资料、已保存的发票和操作审计会保留。
          </p>
          {error && (
            <p className="error-note" role="alert">
              {error}
            </p>
          )}
          <div className="button-row">
            <button className="secondary" disabled={busy} onClick={() => setDeleting(null)}>
              取消
            </button>
            <button
              className="primary"
              disabled={busy}
              onClick={() => void act(deleting, 'delete')}
            >
              {busy ? '正在删除…' : '确认删除'}
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}
