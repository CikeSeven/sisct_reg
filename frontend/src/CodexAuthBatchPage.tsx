import { FormEvent, useEffect, useMemo, useState } from 'react'
import { apiFetch, formatTime } from './lib/api'

type Executor = 'protocol' | 'headless' | 'headed'

type CodexAuthBatchEvent = {
  id: number
  seq: number
  level: string
  account_email: string
  message: string
  created_at: number
}

type CodexAuthBatchResult = {
  email: string
  status: string
  account_id: string
  error: string
  cpa_json: string
  cpa_account: Record<string, unknown>
  created_at: number
}

type CodexAuthBatchSnapshot = {
  id: string
  status: string
  total: number
  success: number
  failed: number
  completed: number
  progress: string
  created_at: number
  events: CodexAuthBatchEvent[]
  results: CodexAuthBatchResult[]
}

type CodexAuthBatchJobListItem = {
  id: string
  status: string
  total: number
  success: number
  failed: number
  completed: number
  progress: string
  created_at: number
}

const ACTIVE_CODEX_AUTH_BATCH_JOB_STORAGE_KEY = 'codex_auth_batch_active_job_id'

export default function CodexAuthBatchPage() {
  const [importText, setImportText] = useState('')
  const [executorType, setExecutorType] = useState<Executor>('protocol')
  const [starting, setStarting] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [error, setError] = useState('')
  const [activeJobId, setActiveJobId] = useState('')
  const [snapshot, setSnapshot] = useState<CodexAuthBatchSnapshot | null>(null)
  const [jobHistory, setJobHistory] = useState<CodexAuthBatchJobListItem[]>([])
  const [selectedEmails, setSelectedEmails] = useState<string[]>([])
  const [exportingZip, setExportingZip] = useState(false)
  const [copyState, setCopyState] = useState('')

  const isRunning = useMemo(() => ['pending', 'running'].includes(snapshot?.status || ''), [snapshot])
  const resultItems = useMemo(() => snapshot?.results || [], [snapshot?.results])
  const successItems = useMemo(() => resultItems.filter((item) => item.status === 'success'), [resultItems])
  const selectedSuccessItems = useMemo(
    () => successItems.filter((item) => selectedEmails.includes(item.email)),
    [successItems, selectedEmails],
  )
  const exportJson = useMemo(
    () => JSON.stringify(selectedSuccessItems.map((item) => item.cpa_account), null, 2),
    [selectedSuccessItems],
  )
  const exportJsonl = useMemo(
    () => selectedSuccessItems.map((item) => item.cpa_json).join('\n'),
    [selectedSuccessItems],
  )

  useEffect(() => {
    const visibleEmails = new Set(successItems.map((item) => item.email).filter(Boolean))
    setSelectedEmails((prev) => prev.filter((email) => visibleEmails.has(email)))
  }, [successItems])

  useEffect(() => {
    const timer = copyState ? window.setTimeout(() => setCopyState(''), 1600) : null
    return () => {
      if (timer) window.clearTimeout(timer)
    }
  }, [copyState])

  async function refreshJobs() {
    const detail = await apiFetch<{ items: CodexAuthBatchJobListItem[] }>('/api/codex-auth-batch/jobs?limit=20')
    const items = detail.items || []
    setJobHistory(items)
    if (!activeJobId && items.length > 0) {
      const latestJobId = String(items[0]?.id || '')
      if (latestJobId) {
        setActiveJobId(latestJobId)
        window.localStorage.setItem(ACTIVE_CODEX_AUTH_BATCH_JOB_STORAGE_KEY, latestJobId)
      }
    }
  }

  async function refreshJob(jobId: string) {
    if (!jobId) return
    const detail = await apiFetch<CodexAuthBatchSnapshot>(`/api/codex-auth-batch/jobs/${jobId}`)
    setSnapshot(detail)
  }

  useEffect(() => {
    const cachedJobId = window.localStorage.getItem(ACTIVE_CODEX_AUTH_BATCH_JOB_STORAGE_KEY) || ''
    if (cachedJobId) {
      setActiveJobId(cachedJobId)
    }
    void refreshJobs()
  }, [])

  useEffect(() => {
    if (!activeJobId) return
    void refreshJob(activeJobId)
    if (!isRunning) return
    const timer = window.setInterval(() => {
      void refreshJob(activeJobId)
      void refreshJobs()
    }, 2000)
    return () => window.clearInterval(timer)
  }, [activeJobId, isRunning])

  async function startJob(event: FormEvent) {
    event.preventDefault()
    if (!importText.trim()) {
      setError('请先粘贴微软邮箱账号')
      return
    }
    setStarting(true)
    setError('')
    try {
      const response = await apiFetch<{ job_id: string }>('/api/codex-auth-batch/jobs', {
        method: 'POST',
        body: JSON.stringify({
          data: importText,
          executor_type: executorType,
        }),
      })
      setActiveJobId(response.job_id)
      window.localStorage.setItem(ACTIVE_CODEX_AUTH_BATCH_JOB_STORAGE_KEY, response.job_id)
      await refreshJobs()
      await refreshJob(response.job_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建批量 Codex 授权任务失败')
    } finally {
      setStarting(false)
    }
  }

  async function stopJob() {
    if (!activeJobId) return
    setStopping(true)
    setError('')
    try {
      await apiFetch(`/api/codex-auth-batch/jobs/${activeJobId}/stop`, { method: 'POST' })
      await refreshJobs()
      await refreshJob(activeJobId)
    } catch (err) {
      setError(err instanceof Error ? err.message : '停止任务失败')
    } finally {
      setStopping(false)
    }
  }

  function toggleEmailSelection(email: string) {
    setSelectedEmails((prev) => (
      prev.includes(email) ? prev.filter((item) => item !== email) : [...prev, email]
    ))
  }

  function selectAllSuccessItems() {
    setSelectedEmails(successItems.map((item) => item.email).filter(Boolean))
  }

  function clearSelectedEmails() {
    setSelectedEmails([])
  }

  async function exportSelectedZip() {
    if (!activeJobId || selectedEmails.length === 0) return
    setExportingZip(true)
    setError('')
    try {
      const response = await fetch(`/api/codex-auth-batch/jobs/${activeJobId}/export`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ emails: selectedEmails }),
      })
      if (!response.ok) {
        throw new Error(await response.text())
      }
      const blob = await response.blob()
      const url = window.URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      const contentDisposition = response.headers.get('Content-Disposition') || ''
      const match = contentDisposition.match(/filename="?([^\"]+)"?/)
      anchor.download = match?.[1] || `codex_auth_batch_${Date.now()}.zip`
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.URL.revokeObjectURL(url)
    } catch (err) {
      setError(err instanceof Error ? err.message : '导出 zip 失败')
    } finally {
      setExportingZip(false)
    }
  }

  async function copyText(value: string, successText: string) {
    if (!value) return
    await navigator.clipboard.writeText(value)
    setCopyState(successText)
  }

  function downloadText(filename: string, content: string, mime = 'application/json;charset=utf-8') {
    if (!content) return
    const blob = new Blob([content], { type: mime })
    const url = window.URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    window.URL.revokeObjectURL(url)
  }

  return (
    <div className="codex-auth-batch-layout">
      {error ? <div className="error-banner">{error}</div> : null}

      <main className="workbench-grid two-pane codex-auth-batch-grid">
        <section className="panel form-panel">
          <div className="panel-title-row">
            <div>
              <h2>批量 Codex 授权</h2>
              <span className="hint">导入已注册 GPT 的微软邮箱，批量完成 Codex OAuth 授权登录并生成 CPA。</span>
            </div>
          </div>

          <form className="config-form" onSubmit={startJob}>
            <div className="sub-block">
              <div className="sub-block-title">导入账号</div>
              <label>
                <span>微软邮箱列表</span>
                <textarea
                  rows={12}
                  value={importText}
                  onChange={(e) => setImportText(e.target.value)}
                  placeholder={'示例：\nuser@example.com----password----client_id----refresh_token'}
                />
              </label>
              <label>
                <span>执行器</span>
                <select value={executorType} onChange={(e) => setExecutorType(e.target.value as Executor)}>
                  <option value="protocol">protocol</option>
                  <option value="headless">headless</option>
                  <option value="headed">headed</option>
                </select>
              </label>
              <div className="helper-note">
                <strong>代理说明</strong>
                <p>该任务始终读取设置页中的代理配置，不使用这里单独传参。</p>
              </div>
              <div className="form-actions split-actions">
                <button className="primary-btn" type="submit" disabled={starting}>
                  {starting ? '启动中...' : '开始批量 Codex 授权'}
                </button>
                <button className="ghost-btn danger" type="button" onClick={() => void stopJob()} disabled={!isRunning || stopping}>
                  {stopping ? '停止中...' : '停止任务'}
                </button>
              </div>
            </div>
          </form>
        </section>

        <section className="panel account-panel codex-auth-batch-result-panel">
          <div className="panel-title-row">
            <h2>任务结果</h2>
            <select
              className="page-size-select"
              value={activeJobId}
              onChange={(e) => {
                const nextJobId = String(e.target.value || '')
                setActiveJobId(nextJobId)
                setSnapshot(null)
                if (nextJobId) {
                  window.localStorage.setItem(ACTIVE_CODEX_AUTH_BATCH_JOB_STORAGE_KEY, nextJobId)
                  void refreshJob(nextJobId)
                } else {
                  window.localStorage.removeItem(ACTIVE_CODEX_AUTH_BATCH_JOB_STORAGE_KEY)
                }
              }}
            >
              {jobHistory.length === 0 ? <option value="">暂无任务</option> : null}
              {jobHistory.map((item) => (
                <option key={item.id} value={item.id}>
                  {`${item.id.slice(0, 18)} · ${item.status} · ${item.progress}`}
                </option>
              ))}
            </select>
          </div>

          <div className="codex-stats-grid">
            <div className="codex-stat tone-default">
              <span>状态</span>
              <strong>{snapshot?.status || '-'}</strong>
            </div>
            <div className="codex-stat tone-default">
              <span>总数</span>
              <strong>{snapshot?.total ?? 0}</strong>
            </div>
            <div className="codex-stat tone-success">
              <span>成功</span>
              <strong>{snapshot?.success ?? 0}</strong>
            </div>
            <div className="codex-stat tone-danger">
              <span>失败</span>
              <strong>{snapshot?.failed ?? 0}</strong>
            </div>
          </div>

          <div className="helper-note codex-progress-note">
            <strong>进度</strong>
            <p>{snapshot?.progress || '0/0'}</p>
          </div>

          <div className="sub-block">
            <div className="panel-title-row codex-auth-batch-title-row">
              <div>
                <div className="sub-block-title">成功结果导出</div>
                <span className="hint">已选中 {selectedEmails.length} / {successItems.length} 个成功账号</span>
              </div>
              {copyState ? <span className="status-chip">{copyState}</span> : null}
            </div>
            <div className="form-actions split-actions codex-auth-batch-select-actions">
              <button className="ghost-btn" type="button" onClick={() => void selectAllSuccessItems()} disabled={successItems.length === 0}>
                全选成功
              </button>
              <button className="ghost-btn" type="button" onClick={() => void clearSelectedEmails()} disabled={selectedEmails.length === 0}>
                清空选择
              </button>
              <div className="selected-count-chip">{selectedEmails.length}</div>
              <button className="ghost-btn" type="button" onClick={() => void copyText(exportJson, '已复制批量 JSON')} disabled={!selectedEmails.length}>
                复制 JSON
              </button>
              <button className="ghost-btn" type="button" onClick={() => void copyText(exportJsonl, '已复制批量 JSONL')} disabled={!selectedEmails.length}>
                复制 JSONL
              </button>
              <button className="ghost-btn danger" type="button" onClick={() => void exportSelectedZip()} disabled={exportingZip || !selectedEmails.length}>
                {exportingZip ? '导出中...' : '导出 ZIP'}
              </button>
            </div>
          </div>

          <div className="codex-team-sections codex-auth-batch-sections">
            <div className="sub-block codex-log-block">
              <div className="sub-block-title">实时日志</div>
              <div className="codex-log-list">
                {(snapshot?.events || []).length === 0 ? <div className="timeline-empty">暂无日志</div> : null}
                {(snapshot?.events || []).map((item) => (
                  <div className={`codex-log-line tone-${item.level === 'error' ? 'danger' : 'default'}`} key={`codex-auth-${item.id}`}>
                    <span>[{item.seq}]</span>
                    <span>{item.account_email ? `${item.account_email} · ` : ''}{item.message}</span>
                  </div>
                ))}
              </div>
            </div>

            <div className="sub-block codex-session-block">
              <div className="sub-block-title">授权结果</div>
              <div className="provider-item-list">
                {resultItems.length === 0 ? <div className="timeline-empty">暂无结果</div> : null}
                {resultItems.map((item) => (
                  <div className="provider-item codex-auth-batch-item" key={`${item.email}-${item.created_at}`}>
                    {item.status === 'success' ? (
                      <label className="account-check codex-session-check">
                        <input
                          type="checkbox"
                          checked={selectedEmails.includes(item.email)}
                          onChange={() => toggleEmailSelection(item.email)}
                        />
                      </label>
                    ) : <div className="codex-auth-batch-check-placeholder" />}
                    <div>
                      <strong>{item.email || '-'}</strong>
                      <p>{item.account_id || item.error || '-'}</p>
                      <p>{formatTime(item.created_at)}</p>
                    </div>
                    <div className="provider-item-meta codex-auth-batch-actions">
                      <span className={`history-badge ${item.status === 'success' ? 'status-done' : 'status-failed'}`}>
                        {item.status}
                      </span>
                      {item.status === 'success' ? (
                        <button className="ghost-btn codex-auth-batch-mini-btn" type="button" onClick={() => void copyText(item.cpa_json, '已复制单条 CPA')}>
                          复制 CPA
                        </button>
                      ) : null}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </section>
      </main>
    </div>
  )
}
