import { useEffect, useRef, useState, type FormEvent } from 'react';
import { api, body, companyName, requestId } from './api';
import { Modal } from './Modal';

export function KnowledgeTextDialog({
  company,
  initialTitle = '',
  initialContent = '',
  requestKey,
  onClose,
  onSaved,
}: {
  company: string;
  initialTitle?: string;
  initialContent?: string;
  requestKey?: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [title, setTitle] = useState(initialTitle);
  const [content, setContent] = useState(initialContent);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const identity = useRef(requestKey ?? requestId());
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      await api('/knowledge/text', {
        method: 'POST',
        body: body({ company_id: company, title, content, request_id: identity.current }),
      });
      if (alive.current) onSaved();
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  return (
    <Modal
      title="添加文字资料"
      onClose={() => {
        if (!busy) onClose();
      }}
    >
      <form className="form-stack" onSubmit={submit}>
        <p className="muted">
          {companyName(company)} ·
          默认仅本人可见。点击提交才入库，索引就绪后可检索；不会改变业务执行规则。
        </p>
        <label>
          资料标题
          <input
            required
            maxLength={200}
            value={title}
            disabled={busy}
            onChange={(e) => {
              setTitle(e.target.value);
              identity.current = requestId();
            }}
          />
        </label>
        <label>
          资料正文
          <textarea
            required
            maxLength={20000}
            rows={12}
            value={content}
            disabled={busy}
            onChange={(e) => {
              setContent(e.target.value);
              identity.current = requestId();
            }}
          />
        </label>
        {error && (
          <p role="alert" className="error-note">
            {error}
          </p>
        )}
        <button className="primary" disabled={busy || !title.trim() || !content.trim()}>
          {busy ? '正在提交…' : '提交到知识库'}
        </button>
      </form>
    </Modal>
  );
}

export function KnowledgeDraftCard({
  company,
  title,
  content,
  requestKey,
}: {
  company: string;
  title: string;
  content: string;
  requestKey: string;
}) {
  const [open, setOpen] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  return (
    <section className="result-card">
      <strong>{title}</strong>
      <p className="muted">
        {submitted ? '已提交接入，请到知识资料查看索引状态。' : '文字入库草稿 · 尚未入库'}
      </p>
      <pre className="source-text">{content}</pre>
      <button className="secondary" disabled={submitted} onClick={() => setOpen(true)}>
        {submitted ? '已提交' : '核对并提交'}
      </button>
      {open && (
        <KnowledgeTextDialog
          company={company}
          initialTitle={title}
          initialContent={content}
          requestKey={requestKey}
          onClose={() => setOpen(false)}
          onSaved={() => {
            setSubmitted(true);
            setOpen(false);
          }}
        />
      )}
    </section>
  );
}
