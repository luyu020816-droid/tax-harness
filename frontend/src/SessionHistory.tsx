import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
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
function SessionActions({ title, children }: { title: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  const trigger = useRef<HTMLDetailsElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const close = () => {
    if (trigger.current) trigger.current.open = false;
  };
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const anchor = trigger.current?.getBoundingClientRect();
      const menu = panel.current?.getBoundingClientRect();
      if (!anchor || !menu) return;
      setPosition({
        left: Math.max(8, Math.min(anchor.right - menu.width, innerWidth - menu.width - 8)),
        top: Math.max(
          8,
          Math.min(
            anchor.bottom + 4 + menu.height <= innerHeight - 8
              ? anchor.bottom + 4
              : anchor.top - menu.height - 4,
            innerHeight - menu.height - 8
          )
        ),
      });
    };
    place();
    const dismiss = (event: PointerEvent) => {
      if (
        !trigger.current?.contains(event.target as Node) &&
        !panel.current?.contains(event.target as Node)
      )
        close();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        close();
        trigger.current?.querySelector('summary')?.focus();
      }
    };
    const scroll = (event: Event) => {
      if (!panel.current?.contains(event.target as Node)) close();
    };
    window.addEventListener('resize', place);
    window.addEventListener('scroll', scroll, true);
    document.addEventListener('pointerdown', dismiss);
    document.addEventListener('keydown', escape);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', scroll, true);
      document.removeEventListener('pointerdown', dismiss);
      document.removeEventListener('keydown', escape);
    };
  }, [open]);
  return (
    <details
      ref={trigger}
      className="session-actions"
      onToggle={(event) => {
        const expanded = event.currentTarget.open;
        setOpen(expanded);
        if (expanded)
          document
            .querySelectorAll<HTMLDetailsElement>('.session-actions[open]')
            .forEach((other) => {
              if (other !== trigger.current) other.open = false;
            });
      }}
    >
      <summary aria-label={`会话操作：${title}`}>
        <MoreHorizontal size={16} />
      </summary>
      {open &&
        createPortal(
          <div ref={panel} className="session-actions-menu" style={position} onClick={close}>
            {children}
          </div>,
          document.body
        )}
    </details>
  );
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
        <SessionActions title={session.title || '新对话'}>
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
        </SessionActions>
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
