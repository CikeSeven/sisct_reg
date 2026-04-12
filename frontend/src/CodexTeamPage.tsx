import { FormEvent, useEffect, useMemo, useState } from 'react'
import { apiFetch, formatTime } from './lib/api'

type Executor = 'protocol' | 'headless' | 'headed'
type CodexTeamParentItem = {
  id: number
  email: string
  enabled: boolean
  has_oauth: boolean
  child_member_count: number
  team_name: string
  team_account_id: string
  last_error: string
}

type CodexTeamParentSummary = {
  total: number
  enabled: number
  disabled: number
  with_oauth: number
  items: CodexTeamParentItem[]
}

type CodexTeamEvent = {
  id: number
  seq: number
  level: string
  account_email: string
  message: string
  created_at: number
}

type CodexTeamSessionItem = {
  id: number
  email: string
  status: string
  selected_workspace_id: string
  selected_workspace_kind: string
  account_id: string
  access_token: string
  refresh_token: string
  id_token: string
  user_id: string
  display_name: string
  info: Record<string, unknown>
  error: string
  created_at: number
}

type CodexTeamJobSnapshot = {
  id: string
  status: string
  total: number
  success: number
  failed: number
  progress: string
  events: CodexTeamEvent[]
  sessions: CodexTeamSessionItem[]
}

const ACTIVE_CODEX_TEAM_JOB_STORAGE_KEY = 'codex_team_active_job_id'

type FormState = {
  max_parent_accounts: number
  target_children_per_parent: number
  concurrency: number
  executor_type: Executor
}

const defaultForm: FormState = {
  max_parent_accounts: 1,
  target_children_per_parent: 5,
  concurrency: 1,
  executor_type: 'protocol',
}

function Stat({ label, value, tone = 'default' }: { label: string; value: string | number; tone?: string }) {
  return (
    <div className={`codex-stat tone-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

export default function CodexTeamPage() {
  const [form, setForm] = useState<FormState>(defaultForm)
  const [starting, setStarting] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [importingParents, setImportingParents] = useState(false)
  const [deletingParentId, setDeletingParentId] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [activeJobId, setActiveJobId] = useState('')
  const [snapshot, setSnapshot] = useState<CodexTeamJobSnapshot | null>(null)
  const [latestSessions, setLatestSessions] = useState<CodexTeamSessionItem[]>([])
  const [parentImportText, setParentImportText] = useState('')
  const [parentSummary, setParentSummary] = useState<CodexTeamParentSummary | null>(null)
  const [exportingCpa, setExportingCpa] = useState(false)
  const [deletingSessionId, setDeletingSessionId] = useState<number | null>(null)
  const [selectedSessionIds, setSelectedSessionIds] = useState<number[]>([])
  const [deletingSelectedSessions, setDeletingSelectedSessions] = useState(false)

  const isRunning = useMemo(() => ['pending', 'running'].includes(snapshot?.status || ''), [snapshot])
  const sessionItems = useMemo(() => (snapshot?.sessions || latestSessions || []), [snapshot?.sessions, latestSessions])
  const selectedSessionCount = selectedSessionIds.length
  const hasSelectedSessions = selectedSessionCount > 0

  useEffect(() => {
    const visibleIds = new Set(sessionItems.map((item) => Number(item.id || 0)).filter((id) => id > 0))
    setSelectedSessionIds((prev) => prev.filter((id) => visibleIds.has(id)))
  }, [sessionItems])

  async function refreshJob(jobId: string) {
    if (!jobId) return
    const detail = await apiFetch<CodexTeamJobSnapshot>(`/api/codex-team/jobs/${jobId}`)
    setSnapshot(detail)
  }

  async function refreshLatestSessions(jobId?: string) {
    const path = jobId ? `/api/codex-team/sessions?job_id=${encodeURIComponent(jobId)}` : '/api/codex-team/sessions'
    const detail = await apiFetch<{ items: CodexTeamSessionItem[] }>(path)
    setLatestSessions(detail.items || [])
  }

  async function refreshParents() {
    const detail = await apiFetch<CodexTeamParentSummary>('/api/codex-team/parents')
    setParentSummary(detail)
    setForm((prev) => ({
      ...prev,
      max_parent_accounts: Math.max(1, detail.enabled || prev.max_parent_accounts || 1),
    }))
  }

  useEffect(() => {
    const cachedJobId = window.localStorage.getItem(ACTIVE_CODEX_TEAM_JOB_STORAGE_KEY) || ''
    if (cachedJobId) {
      setActiveJobId(cachedJobId)
    } else {
      void refreshLatestSessions()
    }
    void refreshParents()
  }, [])

  useEffect(() => {
    if (!activeJobId) return
    void refreshJob(activeJobId)
    void refreshLatestSessions(activeJobId)
    if (!isRunning) return
    const timer = window.setInterval(() => {
      void refreshJob(activeJobId)
      void refreshLatestSessions(activeJobId)
    }, 2000)
    return () => window.clearInterval(timer)
  }, [activeJobId, isRunning])

  async function startJob(event: FormEvent) {
    event.preventDefault()
    setStarting(true)
    setError('')
    try {
      const response = await apiFetch<{ job_id: string }>('/api/codex-team/jobs', {
        method: 'POST',
        body: JSON.stringify({
          parent_source: 'pool',
          max_parent_accounts: form.max_parent_accounts,
          target_children_per_parent: form.target_children_per_parent,
          child_count: form.max_parent_accounts * form.target_children_per_parent,
          concurrency: form.concurrency,
          executor_type: form.executor_type,
        }),
      })
      setActiveJobId(response.job_id)
      window.localStorage.setItem(ACTIVE_CODEX_TEAM_JOB_STORAGE_KEY, response.job_id)
      await refreshJob(response.job_id)
      await refreshLatestSessions(response.job_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建 Codex Team 任务失败')
    } finally {
      setStarting(false)
    }
  }

  async function importParents() {
    setImportingParents(true)
    setError('')
    try {
      await apiFetch('/api/codex-team/parents/import', {
        method: 'POST',
        body: JSON.stringify({
          data: parentImportText,
          enabled: true,
        }),
      })
      setParentImportText('')
      await refreshParents()
    } catch (err) {
      setError(err instanceof Error ? err.message : '导入母号池失败')
    } finally {
      setImportingParents(false)
    }
  }

  async function deleteParent(parentId: number) {
    setDeletingParentId(parentId)
    setError('')
    try {
      await apiFetch(`/api/codex-team/parents/${parentId}`, {
        method: 'DELETE',
      })
      await refreshParents()
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除母号池账号失败')
    } finally {
      setDeletingParentId(null)
    }
  }

  async function stopJob() {
    if (!activeJobId) return
    setStopping(true)
    setError('')
    try {
      await apiFetch(`/api/codex-team/jobs/${activeJobId}/stop`, { method: 'POST' })
      await refreshJob(activeJobId)
      await refreshLatestSessions(activeJobId)
    } catch (err) {
      setError(err instanceof Error ? err.message : '停止任务失败')
    } finally {
      setStopping(false)
    }
  }

  function toggleSession(sessionId: number) {
    setSelectedSessionIds((prev) => (
      prev.includes(sessionId) ? prev.filter((id) => id !== sessionId) : [...prev, sessionId]
    ))
  }

  function selectAllSessions() {
    setSelectedSessionIds(sessionItems.map((item) => Number(item.id || 0)).filter((id) => id > 0))
  }

  function clearSelectedSessions() {
    setSelectedSessionIds([])
  }

  async function exportCpa() {
    setExportingCpa(true)
    setError('')
    try {
      const selectedIds = selectedSessionIds.filter((id) => id > 0)
      const hasCustomSelection = selectedIds.length > 0
      const path = activeJobId
        ? `/api/codex-team/sessions/export-cpa?job_id=${encodeURIComponent(activeJobId)}`
        : '/api/codex-team/sessions/export-cpa'
      const response = await fetch(path, hasCustomSelection ? {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_ids: selectedIds }),
      } : undefined)
      if (!response.ok) {
        throw new Error(await response.text())
      }
      const blob = await response.blob()
      const url = window.URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      const contentDisposition = response.headers.get('Content-Disposition') || ''
      const match = contentDisposition.match(/filename=\"?([^\\\"]+)\"?/)
      anchor.download = match?.[1] || `codex_team_cpa_export_${Date.now()}.zip`
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.URL.revokeObjectURL(url)
    } catch (err) {
      setError(err instanceof Error ? err.message : '导出 CPA 失败')
    } finally {
      setExportingCpa(false)
    }
  }

  async function deleteSession(sessionId: number) {
    setDeletingSessionId(sessionId)
    setError('')
    try {
      await apiFetch(`/api/codex-team/sessions/${sessionId}`, {
        method: 'DELETE',
      })
      if (activeJobId) {
        await refreshJob(activeJobId)
        await refreshLatestSessions(activeJobId)
      } else {
        await refreshLatestSessions()
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除子号结果失败')
    } finally {
      setDeletingSessionId(null)
    }
  }

  async function deleteSelectedSessions() {
    if (!selectedSessionIds.length) return
    setDeletingSelectedSessions(true)
    setError('')
    try {
      await apiFetch('/api/codex-team/sessions/delete-batch', {
        method: 'POST',
        body: JSON.stringify({ session_ids: selectedSessionIds }),
      })
      setSelectedSessionIds([])
      if (activeJobId) {
        await refreshJob(activeJobId)
        await refreshLatestSessions(activeJobId)
      } else {
        await refreshLatestSessions()
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '批量删除子号结果失败')
    } finally {
      setDeletingSelectedSessions(false)
    }
  }

  return (
    <div className="codex-team-layout">
      {error ? <div className="error-banner">{error}</div> : null}
      <section className="panel form-panel">
        <div className="panel-title-row">
          <h2>Codex Team 自动邀请准备</h2>
          <span className="hint">v1：子号登录 / 注册并选择 team workspace</span>
        </div>
        <form className="config-form" onSubmit={startJob}>
          <div className="sub-block">
            <div className="sub-block-title">母号池</div>
            <div className="codex-stats-grid">
              <Stat label="总数" value={parentSummary?.total ?? 0} />
              <Stat label="可用" value={parentSummary?.enabled ?? 0} tone="success" />
              <Stat label="带 OAuth" value={parentSummary?.with_oauth ?? 0} tone="info" />
              <Stat label="停用" value={parentSummary?.disabled ?? 0} tone="danger" />
            </div>
            <label>
              <span>批量导入内容</span>
              <textarea
                rows={6}
                value={parentImportText}
                onChange={(e) => setParentImportText(e.target.value)}
                placeholder={'示例：\\nparent@example.com----access_token----account_id\\nparent@example.com----access_token----account_id----session_token----refresh_token----client_id'}
              />
            </label>
            <div className="form-actions split-actions">
              <button className="primary-btn" type="button" onClick={() => void importParents()} disabled={importingParents}>
                {importingParents ? '导入中...' : '导入母号池'}
              </button>
              <button className="ghost-btn" type="button" onClick={() => void refreshParents()} disabled={importingParents}>
                刷新
              </button>
            </div>
            <div className="helper-note">
              <strong>格式</strong>
              <p>母号池按 team-manage 风格输入 ChatGPT 账号参数：支持 access_token、session_token、refresh_token、client_id、account_id、email 的组合；不再使用微软邮箱重新登录。</p>
            </div>

            <div className="provider-item-list">
              {(parentSummary?.items || []).length === 0 ? <div className="timeline-empty">暂无母号</div> : null}
              {(parentSummary?.items || []).map((item) => (
                <div className="provider-item" key={item.id}>
                  <div>
                    <strong>{item.email}</strong>
                    <p>{item.team_name || '-'}</p>
                    <p>{item.team_account_id || '-'}</p>
                    <p>{`子号数 ${item.child_member_count}`}{item.last_error ? ` · ${item.last_error}` : ''}</p>
                  </div>
                  <div className="provider-item-meta">
                    <span className={`history-badge ${item.enabled ? 'status-done' : 'status-stopped'}`}>
                      {item.enabled ? '可用' : '停用'}
                    </span>
                    <button
                      className="tiny-action-btn danger"
                      type="button"
                      onClick={() => void deleteParent(item.id)}
                      disabled={deletingParentId === item.id}
                    >
                      {deletingParentId === item.id ? '删除中' : '删除'}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="sub-block">
            <div className="sub-block-title">循环邀请任务</div>
            <div className="field-group three-col compact">
              <label>
                <span>母号上限</span>
                <input type="number" min={1} max={1000} value={form.max_parent_accounts} onChange={(e) => setForm((prev) => ({ ...prev, max_parent_accounts: Number(e.target.value) }))} />
              </label>
              <label>
                <span>每个母号目标子号数</span>
                <input type="number" min={1} max={50} value={form.target_children_per_parent} onChange={(e) => setForm((prev) => ({ ...prev, target_children_per_parent: Number(e.target.value) }))} />
              </label>
              <label>
                <span>并发</span>
                <input type="number" min={1} max={100} value={form.concurrency} onChange={(e) => setForm((prev) => ({ ...prev, concurrency: Number(e.target.value) }))} />
              </label>
            </div>
            <label>
              <span>执行器</span>
              <select value={form.executor_type} onChange={(e) => setForm((prev) => ({ ...prev, executor_type: e.target.value as Executor }))}>
                <option value="protocol">protocol</option>
                <option value="headless">headless</option>
                <option value="headed">headed</option>
              </select>
            </label>
            <div className="helper-note">
              <strong>运行策略</strong>
              <p>按母号池顺序处理；每个母号先读取当前 Team 已加入子号数，不足 {form.target_children_per_parent} 个时，从子号池取邮箱继续邀请、注册、授权和落库；达到目标后切下一个母号。</p>
            </div>
          </div>

          <div className="form-actions split-actions">
            <button className="primary-btn" type="submit" disabled={starting || isRunning}>
              {starting ? '创建中...' : '开始任务'}
            </button>
            <button className="ghost-btn danger" type="button" onClick={() => void stopJob()} disabled={!isRunning || stopping}>
              {stopping ? '停止中...' : '停止任务'}
            </button>
          </div>
        </form>
      </section>

      <section className="panel account-panel codex-team-panel">
        <div className="panel-title-row">
          <h2>任务结果</h2>
          <span className="hint">{activeJobId || '暂无任务'}</span>
        </div>

        <div className="codex-stats-grid">
          <Stat label="状态" value={snapshot?.status || '-'} tone={isRunning ? 'info' : 'default'} />
          <Stat label="总数" value={snapshot?.total ?? 0} />
          <Stat label="成功" value={snapshot?.success ?? 0} tone="success" />
          <Stat label="失败" value={snapshot?.failed ?? 0} tone="danger" />
        </div>

        <div className="helper-note codex-progress-note">
          <strong>进度</strong>
          <p>{snapshot?.progress || '0/0'}</p>
        </div>

        <div className="form-actions split-actions">
          <button className="ghost-btn" type="button" onClick={() => void selectAllSessions()} disabled={sessionItems.length === 0}>
            全选
          </button>
          <button className="ghost-btn" type="button" onClick={() => void clearSelectedSessions()} disabled={!hasSelectedSessions}>
            取消选择
          </button>
          <div className="selected-count-chip">{selectedSessionCount}</div>
          <button className="ghost-btn" type="button" onClick={() => void exportCpa()} disabled={exportingCpa || sessionItems.length === 0}>
            {exportingCpa ? '导出中...' : '导出 CPA'}
          </button>
          <button className="ghost-btn danger" type="button" onClick={() => void deleteSelectedSessions()} disabled={deletingSelectedSessions || !hasSelectedSessions}>
            {deletingSelectedSessions ? '删除中...' : '删除选中'}
          </button>
        </div>

        <div className="codex-team-sections">
          <div className="sub-block codex-log-block">
            <div className="sub-block-title">实时日志</div>
            <div className="codex-log-list">
              {(snapshot?.events || []).length === 0 ? <div className="timeline-empty">暂无日志</div> : null}
              {(snapshot?.events || []).map((item) => (
                <div className={`codex-log-line tone-${item.level === 'error' ? 'danger' : 'default'}`} key={item.id}>
                  <span>[{item.seq}]</span>
                  <span>{item.account_email ? `${item.account_email} · ` : ''}{item.message}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="sub-block codex-session-block">
            <div className="sub-block-title">子号会话</div>
            <div className="provider-item-list">
              {sessionItems.length === 0 ? <div className="timeline-empty">暂无结果</div> : null}
              {sessionItems.map((item) => (
                <div className="provider-item codex-session-item" key={item.id}>
                  <label className="account-check codex-session-check">
                    <input
                      type="checkbox"
                      checked={selectedSessionIds.includes(Number(item.id || 0))}
                      onChange={() => toggleSession(Number(item.id || 0))}
                    />
                  </label>
                  <div>
                    <strong>{item.display_name || item.email || '-'}</strong>
                    <p>{item.email || '-'}</p>
                    <p>{item.selected_workspace_id || item.error || '-'}</p>
                    <p>
                      {[
                        (item.info?.plan_type as string | undefined) || '',
                        (item.info?.account_role as string | undefined) || '',
                        item.user_id || '',
                      ].filter(Boolean).join(' · ') || '-'}
                    </p>
                  </div>
                  <div className="provider-item-meta codex-session-meta">
                    <span className={`history-badge ${item.status === 'success' ? 'status-done' : 'status-failed'}`}>
                      {item.status}
                    </span>
                    <span className="hint">{item.selected_workspace_kind || 'no-team'}</span>
                    <span className="hint">{formatTime(item.created_at)}</span>
                    <button
                      className="tiny-action-btn danger"
                      type="button"
                      onClick={() => void deleteSession(item.id)}
                      disabled={deletingSessionId === item.id}
                    >
                      {deletingSessionId === item.id ? '删除中' : '删除'}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>
    </div>
  )
}
