import { useEffect, useMemo, useState } from 'react'
import { apiFetch, formatTime } from './lib/api'

type Executor = 'protocol' | 'headless' | 'headed'

type ImportedAccount = {
  id: string
  email: string
  password: string
  enabled: boolean
}

type GptPasswordCodexEvent = {
  id: number
  seq: number
  level: string
  account_email: string
  message: string
  created_at: number
}

type GptPasswordCodexResult = {
  email: string
  status: string
  account_id: string
  error: string
  cpa_json: string
  cpa_account: Record<string, unknown>
  oauth_account: Record<string, unknown>
  created_at: number
}

type GptPasswordCodexSnapshot = {
  id: string
  status: string
  delimiter: string
  otp_site_url: string
  total: number
  success: number
  failed: number
  completed: number
  progress: string
  created_at: number
  events: GptPasswordCodexEvent[]
  results: GptPasswordCodexResult[]
}

type GptPasswordCodexJobListItem = {
  id: string
  status: string
  delimiter: string
  otp_site_url: string
  total: number
  success: number
  failed: number
  completed: number
  progress: string
  created_at: number
}

const ACTIVE_GPT_PASSWORD_CODEX_JOB_STORAGE_KEY = 'gpt_password_codex_active_job_id'
const DEFAULT_OTP_SITE_URL = 'https://nissanserena.my.id/otp'

function parseImportedAccounts(raw: string, delimiter: string): ImportedAccount[] {
  const delimiterText = String(delimiter || '')
  if (!delimiterText) {
    throw new Error('分隔符不能为空')
  }

  const nextItems: ImportedAccount[] = []
  const seenEmails = new Set<string>()
  String(raw || '')
    .split(/\r?\n/)
    .forEach((rawLine, index) => {
      const line = String(rawLine || '').trim().replace(/^\uFEFF/, '')
      if (!line) return
      const lowered = line.toLowerCase()
      if (line.startsWith('#') || line.startsWith('===')) return
      if (lowered.startsWith('api接码网站') || lowered.startsWith('api otp')) return
      if (!line.includes(delimiterText)) return

      const parts = line.split(delimiterText)
      const email = String(parts.shift() || '').trim().toLowerCase()
      const password = parts.join(delimiterText).trim()
      if (!email && !password) return
      if (!email || !email.includes('@')) {
        throw new Error(`第 ${index + 1} 行邮箱格式无效`)
      }
      if (!password) {
        throw new Error(`第 ${index + 1} 行密码不能为空`)
      }
      if (seenEmails.has(email)) return
      seenEmails.add(email)
      nextItems.push({
        id: `${email}-${index}`,
        email,
        password,
        enabled: true,
      })
    })

  if (nextItems.length === 0) {
    throw new Error('未解析到任何账号，请检查导入文本或分隔符')
  }
  return nextItems
}

export default function GptPasswordCodexPage() {
  const [importText, setImportText] = useState('')
  const [delimiter, setDelimiter] = useState('|')
  const [otpSiteUrl, setOtpSiteUrl] = useState(DEFAULT_OTP_SITE_URL)
  const [executorType, setExecutorType] = useState<Executor>('protocol')
  const [importedAccounts, setImportedAccounts] = useState<ImportedAccount[]>([])
  const [starting, setStarting] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [error, setError] = useState('')
  const [activeJobId, setActiveJobId] = useState('')
  const [snapshot, setSnapshot] = useState<GptPasswordCodexSnapshot | null>(null)
  const [jobHistory, setJobHistory] = useState<GptPasswordCodexJobListItem[]>([])
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
  const enabledImportedAccounts = useMemo(
    () => importedAccounts.filter((item) => item.enabled && item.email && item.password),
    [importedAccounts],
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
    const detail = await apiFetch<{ items: GptPasswordCodexJobListItem[] }>('/api/gpt-password-codex/jobs?limit=20')
    const items = detail.items || []
    setJobHistory(items)
    if (!activeJobId && items.length > 0) {
      const latestJobId = String(items[0]?.id || '')
      if (latestJobId) {
        setActiveJobId(latestJobId)
        window.localStorage.setItem(ACTIVE_GPT_PASSWORD_CODEX_JOB_STORAGE_KEY, latestJobId)
      }
    }
  }

  async function refreshJob(jobId: string) {
    if (!jobId) return
    const detail = await apiFetch<GptPasswordCodexSnapshot>(`/api/gpt-password-codex/jobs/${jobId}`)
    setSnapshot(detail)
  }

  useEffect(() => {
    const cachedJobId = window.localStorage.getItem(ACTIVE_GPT_PASSWORD_CODEX_JOB_STORAGE_KEY) || ''
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

  function importAccounts() {
    setError('')
    try {
      const parsed = parseImportedAccounts(importText, delimiter)
      setImportedAccounts(parsed)
    } catch (err) {
      setError(err instanceof Error ? err.message : '导入账号失败')
    }
  }

  async function startJob() {
    if (enabledImportedAccounts.length === 0) {
      setError('请先导入账号，并至少保留 1 个启用状态的账号')
      return
    }
    if (!delimiter) {
      setError('分隔符不能为空')
      return
    }
    if (!otpSiteUrl.trim()) {
      setError('接码站地址不能为空')
      return
    }

    setStarting(true)
    setError('')
    try {
      const payloadText = enabledImportedAccounts
        .map((item) => `${item.email}${delimiter}${item.password}`)
        .join('\n')
      const response = await apiFetch<{ job_id: string }>('/api/gpt-password-codex/jobs', {
        method: 'POST',
        body: JSON.stringify({
          data: payloadText,
          delimiter,
          otp_site_url: otpSiteUrl,
          executor_type: executorType,
        }),
      })
      setActiveJobId(response.job_id)
      window.localStorage.setItem(ACTIVE_GPT_PASSWORD_CODEX_JOB_STORAGE_KEY, response.job_id)
      await refreshJobs()
      await refreshJob(response.job_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建 GPT 账号 Codex 转换任务失败')
    } finally {
      setStarting(false)
    }
  }

  async function stopJob() {
    if (!activeJobId) return
    setStopping(true)
    setError('')
    try {
      await apiFetch(`/api/gpt-password-codex/jobs/${activeJobId}/stop`, { method: 'POST' })
      await refreshJobs()
      await refreshJob(activeJobId)
    } catch (err) {
      setError(err instanceof Error ? err.message : '停止任务失败')
    } finally {
      setStopping(false)
    }
  }
  async function deleteConvertedResult(email: string) {
    if (!activeJobId || !email) return
    setError('')
    try {
      await apiFetch(`/api/gpt-password-codex/jobs/${activeJobId}/results/delete`, {
        method: 'POST',
        body: JSON.stringify({ emails: [email] }),
      })
      setSelectedEmails((prev) => prev.filter((item) => item !== email))
      await refreshJobs()
      await refreshJob(activeJobId)
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除转换结果失败')
    }
  }


  function toggleImportEnabled(id: string) {
    setImportedAccounts((prev) => prev.map((item) => (item.id === id ? { ...item, enabled: !item.enabled } : item)))
  }

  function updateImportedPassword(id: string, password: string) {
    setImportedAccounts((prev) => prev.map((item) => (item.id === id ? { ...item, password } : item)))
  }

  function removeImportedAccount(id: string) {
    setImportedAccounts((prev) => prev.filter((item) => item.id !== id))
  }

  function setAllImportedEnabled(enabled: boolean) {
    setImportedAccounts((prev) => prev.map((item) => ({ ...item, enabled })))
  }

  function clearImportedAccounts() {
    setImportedAccounts([])
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
      const response = await fetch(`/api/gpt-password-codex/jobs/${activeJobId}/export`, {
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
      anchor.download = match?.[1] || `gpt_password_codex_${Date.now()}.zip`
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
              <h2>GPT 账号转 Codex</h2>
              <span className="hint">先导入账号到列表，再管理启用状态和接码地址，最后开始批量 Codex OAuth 授权并导出 CPA / Sub2API。</span>
            </div>
          </div>

          <div className="config-form">
            <div className="sub-block">
              <div className="sub-block-title">步骤 1：填充并导入账号</div>
              <label>
                <span>原始账号文本</span>
                <textarea
                  rows={12}
                  value={importText}
                  onChange={(e) => setImportText(e.target.value)}
                  placeholder={'支持带说明文本一起粘贴，例如：\n=== 使用说明 ===\napi接码网站 https://nissanserena.my.id/otp\n\n=== 卡密内容 ===\nuser@example.com|password123'}
                />
              </label>
              <label>
                <span>分隔符</span>
                <input value={delimiter} onChange={(e) => setDelimiter(e.target.value)} placeholder="默认 |，支持自定义如 ----" />
              </label>
              <label>
                <span>接码站地址</span>
                <input value={otpSiteUrl} onChange={(e) => setOtpSiteUrl(e.target.value)} placeholder={DEFAULT_OTP_SITE_URL} />
              </label>
              <div className="helper-note">
                <strong>说明</strong>
                <p>点击“导入到列表”后，不会立即开始转换，只会先解析账号。</p>
                <p>前置说明行和“=== 卡密内容 ===”会自动跳过，接码查询默认请求 <code>{DEFAULT_OTP_SITE_URL}/search?email=...</code>。</p>
              </div>
              <div className="form-actions split-actions">
                <button className="primary-btn" type="button" onClick={importAccounts}>
                  导入到列表
                </button>
                <button className="ghost-btn" type="button" onClick={() => setImportText('')} disabled={!importText.trim()}>
                  清空输入
                </button>
              </div>
            </div>

            <div className="sub-block">
              <div className="panel-title-row codex-auth-batch-title-row">
                <div>
                  <div className="sub-block-title">步骤 2：管理导入账号</div>
                  <span className="hint">当前共 {importedAccounts.length} 个，启用 {enabledImportedAccounts.length} 个</span>
                </div>
              </div>
              <label>
                <span>执行器</span>
                <select value={executorType} onChange={(e) => setExecutorType(e.target.value as Executor)}>
                  <option value="protocol">protocol</option>
                  <option value="headless">headless</option>
                  <option value="headed">headed</option>
                </select>
              </label>
              <div className="helper-note codex-progress-note">
                <strong>当前配置</strong>
                <p>分隔符：{delimiter || '-'}</p>
                <p>接码站：{otpSiteUrl || '-'}</p>
              </div>
              <div className="form-actions split-actions codex-auth-batch-select-actions">
                <button className="ghost-btn" type="button" onClick={() => setAllImportedEnabled(true)} disabled={importedAccounts.length === 0}>
                  全部启用
                </button>
                <button className="ghost-btn" type="button" onClick={() => setAllImportedEnabled(false)} disabled={importedAccounts.length === 0}>
                  全部停用
                </button>
                <button className="ghost-btn danger" type="button" onClick={clearImportedAccounts} disabled={importedAccounts.length === 0}>
                  清空列表
                </button>
              </div>
              <div className="provider-item-list codex-auth-batch-success-list">
                {importedAccounts.length === 0 ? <div className="empty-state compact">还没有导入账号</div> : null}
                {importedAccounts.map((item) => (
                  <div key={item.id} className={`provider-item selectable ${item.enabled ? 'is-success' : ''}`}>
                    <div className="provider-check-row" style={{ alignItems: 'flex-start' }}>
                      <input type="checkbox" checked={item.enabled} onChange={() => toggleImportEnabled(item.id)} />
                      <div style={{ flex: 1 }}>
                        <strong>{item.email}</strong>
                        <div style={{ marginTop: 8 }}>
                          <input
                            value={item.password}
                            onChange={(e) => updateImportedPassword(item.id, e.target.value)}
                            placeholder="密码"
                          />
                        </div>
                        <p className="hint">状态：{item.enabled ? '启用，将参与转换' : '停用，不参与转换'}</p>
                      </div>
                      <button className="ghost-btn danger oauth-mini-btn" type="button" onClick={() => removeImportedAccount(item.id)}>
                        删除
                      </button>
                    </div>
                  </div>
                ))}
              </div>
              <div className="form-actions split-actions">
                <button className="primary-btn" type="button" onClick={() => void startJob()} disabled={starting || enabledImportedAccounts.length === 0}>
                  {starting ? '启动中...' : '开始转换'}
                </button>
                <button className="ghost-btn danger" type="button" onClick={() => void stopJob()} disabled={!isRunning || stopping}>
                  {stopping ? '停止中...' : '停止任务'}
                </button>
              </div>
            </div>
          </div>
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
                  window.localStorage.setItem(ACTIVE_GPT_PASSWORD_CODEX_JOB_STORAGE_KEY, nextJobId)
                  void refreshJob(nextJobId)
                } else {
                  window.localStorage.removeItem(ACTIVE_GPT_PASSWORD_CODEX_JOB_STORAGE_KEY)
                }
              }}
            >
              {jobHistory.length === 0 ? <option value="">暂无任务</option> : null}
              {jobHistory.map((item) => (
                <option key={item.id} value={item.id}>
                  {`${item.id.slice(0, 24)} · ${item.status} · ${item.progress}`}
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
            <p>分隔符：{snapshot?.delimiter || delimiter || '-'}</p>
            <p>接码站：{snapshot?.otp_site_url || otpSiteUrl || '-'}</p>
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
              <button className="ghost-btn" type="button" onClick={() => downloadText('gpt_password_codex_accounts.json', exportJson)} disabled={!selectedEmails.length}>
                下载 JSON
              </button>
              <button className="ghost-btn danger" type="button" onClick={() => void exportSelectedZip()} disabled={exportingZip || !selectedEmails.length}>
                {exportingZip ? '导出中...' : '导出 ZIP'}
              </button>
            </div>
          </div>

          <div className="provider-item-list codex-auth-batch-success-list">
            {resultItems.length === 0 ? <div className="empty-state compact">暂无结果</div> : null}
            {resultItems.map((item) => {
              const checked = selectedEmails.includes(item.email)
              return (
                <div key={`${item.email}-${item.created_at}`} className={`provider-item selectable ${item.status === 'success' ? 'is-success' : 'is-danger'}`}>
                  <div className="provider-check-row">
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={item.status !== 'success'}
                      onChange={() => item.status === 'success' && toggleEmailSelection(item.email)}
                    />
                    <div style={{ flex: 1 }}>
                      <strong>{item.email || '-'}</strong>
                      <p>{item.status === 'success' ? `Account ID: ${item.account_id || '-'}` : item.error || '转换失败'}</p>
                      <p className="hint">{formatTime(item.created_at)}</p>
                    </div>
                    <button className="ghost-btn danger oauth-mini-btn" type="button" onClick={() => void deleteConvertedResult(item.email)}>
                      删除
                    </button>
                  </div>
                </div>
              )
            })}
          </div>

          <div className="sub-block">
            <div className="sub-block-title">任务日志</div>
            <div className="log-console compact">
              {(snapshot?.events || []).slice(-120).map((event) => (
                <div key={`${event.id}-${event.seq}`} className={`log-line ${event.level === 'error' ? 'error' : event.level === 'warning' ? 'warning' : ''}`}>
                  <span className="log-time">[{formatTime(event.created_at)}]</span>
                  <span className="log-msg">{event.account_email ? `${event.account_email} · ` : ''}{event.message}</span>
                </div>
              ))}
            </div>
          </div>
        </section>
      </main>
    </div>
  )
}
