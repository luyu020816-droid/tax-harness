import { useEffect, useRef, useState, type FormEvent } from 'react';
import { BookOpen, FilePlus2, LoaderCircle, RefreshCw, Search } from 'lucide-react';
import { api, body, companyName, requestId } from './api';
import { KnowledgeCitation } from './KnowledgeCitation';
import { Modal } from './Modal';
import { useResource } from './useResource';
import type {
  Detail,
  KnowledgeAttachment,
  KnowledgeBundle,
  KnowledgeDocument,
  User,
} from './types';

export const stageName = (state: string) =>
  ({
    queued: '等待接入',
    parsing: '解析正文',
    chunking: '整理片段',
    embedding: '建立索引',
    validating: '核对完整性',
    ready: '已就绪',
    blocked: '等待依赖',
    failed: '接入失败',
    cancelled: '已取消',
  })[state] ?? state;

function UploadKnowledge({
  company,
  target,
  onClose,
  onSaved,
}: {
  company: string;
  target: KnowledgeDocument | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState(target?.title ?? '');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const identity = useRef(requestId());
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!file) return;
    if (file.size > 20 * 1024 * 1024) {
      setError('文件最多20 MiB。');
      return;
    }
    setBusy(true);
    setError('');
    const form = new FormData();
    form.append('file', file);
    form.append('title', title);
    form.append('company_id', company);
    form.append('request_id', identity.current);
    if (target) form.append('expected_document_revision', String(target.revision));
    if (from) form.append('effective_from', from);
    if (to) form.append('effective_to', to);
    try {
      await api(
        target ? `/knowledge/documents/${target.document_id}/versions` : '/knowledge/uploads',
        { method: 'POST', body: form }
      );
      if (alive.current) {
        onSaved();
        onClose();
      }
    } catch (err) {
      if (alive.current) setError((err as Error).message);
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  const edited = () => {
    identity.current = requestId();
  };
  return (
    <Modal
      title={target ? '上传资料新版本' : '上传知识资料'}
      onClose={() => {
        if (!busy) onClose();
      }}
    >
      <form className="form-stack" onSubmit={submit}>
        <p className="muted">
          {companyName(company)} · 默认仅本人可见。完成解析和索引后才可引用；新版本失败会保留旧版。
        </p>
        <label>
          文件
          <input
            type="file"
            accept=".pdf,.docx,.md,.txt"
            required
            disabled={busy}
            onChange={(event) => {
              const next = event.target.files?.[0] ?? null;
              setFile(next);
              if (!title && next) setTitle(next.name.replace(/\.[^.]+$/, ''));
              edited();
            }}
          />
        </label>
        <label>
          资料标题
          <input
            value={title}
            maxLength={200}
            required
            disabled={busy || Boolean(target)}
            onChange={(event) => {
              setTitle(event.target.value);
              edited();
            }}
          />
        </label>
        <div className="knowledge-date-fields">
          <label>
            生效日期（可选）
            <input
              type="date"
              value={from}
              disabled={busy}
              onChange={(event) => {
                setFrom(event.target.value);
                edited();
              }}
            />
          </label>
          <label>
            结束日期（不含当天，可选）
            <input
              type="date"
              value={to}
              disabled={busy}
              onChange={(event) => {
                setTo(event.target.value);
                edited();
              }}
            />
          </label>
        </div>
        <p className="muted small">
          支持文本PDF、DOCX、Markdown和TXT；扫描页需要后续OCR支持。资料用于检索问答，不会自动变更业务规则。
        </p>
        {error && (
          <p className="error-note" role="alert">
            {error}
          </p>
        )}
        <footer className="form-actions">
          <button type="button" className="secondary" disabled={busy} onClick={onClose}>
            取消
          </button>
          <button className="primary" disabled={busy || !file || !title.trim()}>
            {busy ? '正在受理…' : '上传并建立索引'}
          </button>
        </footer>
      </form>
    </Modal>
  );
}

export function KnowledgeView({
  user,
  company,
  sessionId,
  onDetail,
  onAttach,
  onDraft,
}: {
  user: User;
  company: string | null;
  sessionId: string | null;
  onDetail: (detail: Detail) => void;
  onAttach: (file: KnowledgeAttachment) => void;
  onDraft: (text: string) => void;
}) {
  const documents = useResource<{
    documents: KnowledgeDocument[];
    has_more: boolean;
    next_offset: number | null;
  }>(company ? `/knowledge/documents?company_id=${company}` : null);
  const [uploading, setUploading] = useState(false);
  const [target, setTarget] = useState<KnowledgeDocument | null>(null);
  const [query, setQuery] = useState('');
  const [bundle, setBundle] = useState<KnowledgeBundle | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [older, setOlder] = useState<KnowledgeDocument[]>([]);
  const [firstPage, setFirstPage] = useState<KnowledgeDocument[]>([]);
  const [hasMore, setHasMore] = useState<boolean | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const alive = useRef(true);
  const searchController = useRef<AbortController | null>(null);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      searchController.current?.abort();
    };
  }, []);
  useEffect(() => {
    if (documents.data) setFirstPage(documents.data.documents);
  }, [documents.data]);
  const list = [
    ...new Map(
      [...(documents.data?.documents ?? firstPage), ...older].map((d) => [d.document_id, d])
    ).values(),
  ];
  const pending = list.some(
    (d) =>
      d.latest &&
      ['queued', 'parsing', 'chunking', 'embedding', 'validating'].includes(d.latest.state)
  );
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(documents.reload, 1500);
    return () => clearInterval(timer);
  }, [pending]);
  if (!company)
    return (
      <section className="knowledge-page">
        <h1>知识资料</h1>
        <p className="muted">请先选择要处理的企业。</p>
      </section>
    );
  const canUpload =
    user.companies.find((c) => c.company_id === company)?.can_upload_knowledge ?? false;

  function reload() {
    setBundle(null);
    setOlder([]);
    setHasMore(null);
    documents.reload();
  }
  async function search(event: FormEvent) {
    event.preventDefault();
    searchController.current?.abort();
    const controller = new AbortController();
    searchController.current = controller;
    setBusy(true);
    setError('');
    setBundle(null);
    try {
      const result = await api<KnowledgeBundle>('/retrieval/search', {
        method: 'POST',
        signal: controller.signal,
        body: body({ company_id: company, query, session_id: sessionId }),
      });
      if (alive.current && !controller.signal.aborted) setBundle(result);
    } catch (err) {
      if (alive.current && !controller.signal.aborted) setError((err as Error).message);
    } finally {
      if (alive.current && !controller.signal.aborted) setBusy(false);
    }
  }
  async function action(document: KnowledgeDocument, type: 'disable' | 'publish' | 'retry') {
    setError('');
    try {
      if (type === 'retry')
        await api(`/knowledge/jobs/${document.latest!.job_id}/retry`, {
          method: 'POST',
          body: body({ request_id: requestId() }),
        });
      else
        await api(`/knowledge/documents/${document.document_id}/${type}`, {
          method: 'POST',
          body: body({
            request_id: requestId(),
            expected_document_revision: document.revision,
            ...(type === 'publish' ? { version_id: document.latest!.version_id } : {}),
          }),
        });
      if (alive.current) reload();
    } catch (err) {
      if (alive.current) setError((err as Error).message);
    }
  }
  async function ask(document: KnowledgeDocument) {
    if (document.scope === 'company') {
      onDraft(`请查阅${companyName(company!)}《${document.title}》，`);
      return;
    }
    try {
      const detail = await api<KnowledgeDocument>(`/knowledge/documents/${document.document_id}`);
      if (!alive.current) return;
      const version = detail.versions?.find((v) => v.version_id === detail.active_version_id);
      if (!version) throw new Error('资料尚未完成接入。');
      onAttach({
        file_id: version.file_id,
        document_id: detail.document_id,
        company_id: company!,
        title: detail.title,
        original_name: version.original_name,
      });
    } catch (err) {
      if (alive.current) setError((err as Error).message);
    }
  }
  return (
    <section className="knowledge-page">
      <header className="page-heading">
        <div>
          <h1>知识资料</h1>
          <p>查阅{companyName(company)}制度与参考资料，保留原文和来源。</p>
        </div>
        <button
          className="primary"
          disabled={!canUpload}
          onClick={() => {
            setTarget(null);
            setUploading(true);
          }}
        >
          <FilePlus2 size={16} />
          上传资料
        </button>
      </header>
      <form className="knowledge-search" onSubmit={search}>
        <Search size={18} />
        <input
          aria-label="检索知识资料"
          value={query}
          required
          maxLength={2000}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="例如：出差住宿需要谁审批？"
        />
        <button className="secondary" disabled={busy || !query.trim()}>
          {busy ? <LoaderCircle size={16} className="spinning" /> : '查找依据'}
        </button>
      </form>
      {error && (
        <p className="error-note" role="alert">
          {error}
        </p>
      )}
      {bundle && (
        <div className="knowledge-results" aria-live="polite">
          <p className="muted">
            {bundle.status === 'unavailable'
              ? '索引或检索依赖未就绪，请查看接入状态；这不代表没有资料。'
              : bundle.status === 'not_found'
                ? '当前授权范围与有效版本中没有找到可引用片段。'
                : bundle.status === 'partial'
                  ? '部分资料或证据尚未完整覆盖，以下为可核对的原文。'
                  : '已找到可引用片段。'}
            {bundle.retrieval_mode === 'lexical_only' && ' 本次仅按文字查找。'}
            {bundle.retrieval_mode === 'hybrid_unranked' && ' 本次重排暂不可用。'}
          </p>
          {bundle.evidence.map((evidence) => (
            <KnowledgeCitation
              key={evidence.citation_id}
              id={evidence.citation_id}
              onDetail={onDetail}
            />
          ))}
        </div>
      )}
      <div className="knowledge-list-heading">
        <h2>当前资料</h2>
        <button className="text-button" onClick={reload}>
          <RefreshCw size={14} />
          刷新
        </button>
      </div>
      {documents.error && <p className="error-note">{documents.error}</p>}
      {documents.loading && !list.length ? (
        <p className="muted">正在读取资料…</p>
      ) : !list.length ? (
        <div className="empty-panel">
          <BookOpen size={30} />
          <h3>还没有可见的知识资料</h3>
          <p>上传制度或文本PDF，完成索引后即可检索、引用。</p>
        </div>
      ) : (
        <div className="memory-list">
          {list.map((document) => (
            <article className="memory-row knowledge-document" key={document.document_id}>
              <div className="memory-content">
                <button
                  className="text-button"
                  onClick={() => onDetail({ kind: 'knowledge', id: document.document_id })}
                >
                  <strong>{document.title}</strong>
                </button>
                <p className="muted small">
                  {document.scope === 'company' ? '企业共享' : '仅本人'} ·{' '}
                  {document.status === 'disabled'
                    ? '已停用'
                    : document.active_edition
                      ? `当前第${document.active_edition}版`
                      : '尚未发布'}
                  {document.latest &&
                    document.latest.version_id !== document.active_version_id &&
                    ` · 新版本${stageName(document.latest.state)}`}
                </p>
                {document.latest?.error && (
                  <p className="error-note small">
                    {document.latest.error.message}
                    {document.active_version_id && ' 当前旧版本仍可使用。'}
                  </p>
                )}
              </div>
              <div className="row-actions">
                <button
                  className="text-button"
                  disabled={!document.active_version_id || document.status !== 'active'}
                  onClick={() => void ask(document)}
                >
                  用于对话
                </button>
                {document.scope === 'user_private' && document.owner_id === user.user_id && (
                  <>
                    <button
                      className="text-button"
                      disabled={!canUpload}
                      onClick={() => {
                        setTarget(document);
                        setUploading(true);
                      }}
                    >
                      上传新版
                    </button>
                    {document.latest?.state === 'ready' &&
                      (document.latest.version_id !== document.active_version_id ||
                        document.status === 'disabled') && (
                        <button
                          className="text-button"
                          onClick={() => void action(document, 'publish')}
                        >
                          发布此版本
                        </button>
                      )}
                    {document.latest && ['failed', 'blocked'].includes(document.latest.state) && (
                      <button
                        className="text-button"
                        onClick={() => void action(document, 'retry')}
                      >
                        重试接入
                      </button>
                    )}
                    {document.status === 'active' && (
                      <button
                        className="text-button muted"
                        onClick={() => void action(document, 'disable')}
                      >
                        停用
                      </button>
                    )}
                  </>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
      {(hasMore ?? documents.data?.has_more) && (
        <button
          className="text-button"
          disabled={loadingMore}
          onClick={async () => {
            setLoadingMore(true);
            try {
              const next = await api<{ documents: KnowledgeDocument[]; has_more: boolean }>(
                `/knowledge/documents?company_id=${company}&offset=${list.length}`
              );
              if (alive.current) {
                setOlder((previous) => [...previous, ...next.documents]);
                setHasMore(next.has_more);
              }
            } catch (err) {
              if (alive.current) setError((err as Error).message);
            } finally {
              if (alive.current) setLoadingMore(false);
            }
          }}
        >
          加载更多资料
        </button>
      )}
      <p className="knowledge-footnote muted small">
        新资料默认私有；企业共享由维护者发布。资料上传不自动改变财税业务规则。
      </p>
      {uploading && (
        <UploadKnowledge
          company={company}
          target={target}
          onClose={() => setUploading(false)}
          onSaved={reload}
        />
      )}
    </section>
  );
}
