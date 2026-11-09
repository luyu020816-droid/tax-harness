import { useEffect, useState, type FormEvent } from 'react';
import { api, body } from './api';
import { Modal } from './Modal';

type ConnectionStatus = {
  configured: boolean;
  source: 'none' | 'env' | 'page';
  model_id: string | null;
  test_status: 'unconfigured' | 'untested' | 'success' | 'failed';
};

const statusLabels = {
  unconfigured: '未配置',
  untested: '已配置，待测试',
  success: '连接成功',
  failed: '连接失败',
};

export function ModelConnection({
  onClose,
  onEnabled,
}: {
  onClose: () => void;
  onEnabled: () => void;
}) {
  const [connection, setConnection] = useState<ConnectionStatus | null>(null);
  const [baseUrl, setBaseUrl] = useState('');
  const [modelId, setModelId] = useState('');
  const [key, setKey] = useState('');
  const [edited, setEdited] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const hasDraft = Boolean(baseUrl || modelId || key);

  useEffect(() => {
    const controller = new AbortController();
    api<ConnectionStatus>('/model-connection', { signal: controller.signal })
      .then((value) => {
        setConnection(value);
      })
      .catch((err: Error) => {
        if (!controller.signal.aborted) setError(err.message);
      });
    return () => controller.abort();
  }, []);

  function edit() {
    setEdited(true);
    setError('');
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!connection || busy) return;
    const payload = body(
      hasDraft ? { base_url: baseUrl.trim(), model_id: modelId.trim(), api_key: key } : {}
    );
    setKey('');
    setBusy(true);
    setEdited(false);
    setError('');
    try {
      const updated = await api<ConnectionStatus>('/model-connection/test', {
        method: 'POST',
        body: payload,
      });
      setConnection(updated);
      setBaseUrl('');
      setModelId('');
      onEnabled();
    } catch (err) {
      setConnection({ ...connection, test_status: 'failed' });
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="本机模型设置" onClose={() => !busy && onClose()}>
      <form className="form-stack" onSubmit={submit}>
        <div role="status" aria-live="polite">
          <strong>
            {busy
              ? '正在测试连接'
              : edited
                ? '待测试'
                : connection
                  ? statusLabels[connection.test_status]
                  : error
                    ? '设置不可用'
                    : '正在读取配置'}
          </strong>
          {connection?.configured && (
            <p className="muted small">
              当前使用 {connection.model_id}（{connection.source === 'env' ? '.env' : '页面配置'}）
            </p>
          )}
        </div>
        <label>
          API 地址
          <input
            type="url"
            required={hasDraft}
            disabled={busy}
            maxLength={500}
            value={baseUrl}
            placeholder="https://api.example.com/v1"
            onChange={(event) => {
              setBaseUrl(event.target.value);
              edit();
            }}
          />
        </label>
        <label>
          模型名称
          <input
            required={hasDraft}
            disabled={busy}
            maxLength={200}
            value={modelId}
            placeholder={connection?.model_id ?? '供应商提供的模型名称'}
            onChange={(event) => {
              setModelId(event.target.value);
              edit();
            }}
          />
        </label>
        <label>
          API Key
          <input
            type="password"
            autoComplete="new-password"
            required={hasDraft}
            disabled={busy}
            maxLength={2048}
            value={key}
            onChange={(event) => {
              setKey(event.target.value);
              edit();
            }}
          />
        </label>
        <p className="muted small">测试通过后启用，服务重启后重新读取 .env。</p>
        {error && (
          <p className="error-note" role="alert">
            {error}
            {connection?.configured && ' 当前配置已保留。'}
          </p>
        )}
        <div className="form-actions">
          <button
            type="submit"
            className="primary-button"
            disabled={
              !connection ||
              busy ||
              (hasDraft
                ? !baseUrl.trim() || !modelId.trim() || !key.trim()
                : !connection.configured)
            }
          >
            {busy ? '测试中…' : hasDraft || !connection?.configured ? '测试并启用' : '测试连接'}
          </button>
        </div>
      </form>
    </Modal>
  );
}
