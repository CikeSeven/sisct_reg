import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { apiFetch, formatTime } from './lib/api'

type Executor = 'protocol' | 'headless' | 'headed'

type TeamOpenConfig = {
  team_open_precheck_enabled: boolean
  team_open_precheck_attempts: number
  team_open_auto_submit_payment: boolean
  team_open_reuse_cards: boolean
  team_open_use_proxy: boolean
  team_open_proxy: string
  team_open_payment_service_base_url: string
  team_open_payment_service_plan_name: string
  team_open_payment_service_country: string
  team_open_payment_service_currency: string
  team_open_payment_service_promo_campaign_id: string
  team_open_payment_service_price_interval: string
  team_open_payment_service_seat_quantity: number
  team_open_payment_service_check_card_proxy: boolean
  team_open_payment_service_is_short_link: boolean
  team_open_pro_checkout_country: string
  team_open_pro_checkout_currency: string
  team_open_default_holder_name: string
  team_open_default_billing_email: string
  team_open_default_country: string
  team_open_default_state: string
  team_open_default_city: string
  team_open_default_line1: string
  team_open_default_postal_code: string
  team_open_post_payment_wait_seconds: number
  team_open_verification_retries: number
}

type TeamOpenCardItem = {
  id: number
  label: string
  last4: string
  masked_number: string
  exp_month: string
  exp_year: string
  holder_name: string
  billing_email: string
  country: string
  state: string
  city: string
  line1: string
  postal_code: string
  enabled: boolean
  success_count: number
  failure_count: number
  last_error: string
  last_used: number
}

type TeamOpenCardSummary = {
  total: number
  enabled: number
  disabled: number
  success_count: number
  failure_count: number
  items: TeamOpenCardItem[]
}

type TeamOpenEvent = {
  id: number
  seq: number
  level: string
  account_email: string
  message: string
  created_at: number
}

type TeamOpenResult = {
  id: number
  attempt_index: number
  email: string
  password: string
  status: string
  card_id: number
  card_label: string
  precheck_status: string
  payment_status: string
  payment_url: string
  payment_service: string
  team_account_id: string
  team_name: string
  imported_parent_id: number
  error: string
  extra_json: Record<string, unknown>
  created_at: number
  updated_at: number
}

type TeamOpenJobSnapshot = {
  id: string
  status: string
  total: number
  success: number
  failed: number
  completed: number
  progress: string
  created_at: number
  updated_at: number
  request_json: Record<string, unknown>
  events: TeamOpenEvent[]
  results: TeamOpenResult[]
  card_summary?: TeamOpenCardSummary
}

type TeamOpenJobListItem = {
  id: string
  status: string
  total: number
  success: number
  failed: number
  completed: number
  progress: string
  created_at: number
}

const ACTIVE_TEAM_OPEN_JOB_STORAGE_KEY = 'team_open_active_job_id'
const TEAM_OPEN_FORM_DRAFT_STORAGE_KEY = 'team_open_form_draft'

type TeamOpenFormDraft = {
  count?: number
  concurrency?: number
  executorType?: Executor
  config?: Partial<TeamOpenConfig>
}

function clampNumber(value: unknown, fallback: number, min: number, max: number) {
  const numberValue = Number(value)
  if (!Number.isFinite(numberValue)) return fallback
  return Math.min(max, Math.max(min, Math.trunc(numberValue)))
}

function readTeamOpenFormDraft(): TeamOpenFormDraft {
  try {
    const raw = window.localStorage.getItem(TEAM_OPEN_FORM_DRAFT_STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? (parsed as TeamOpenFormDraft) : {}
  } catch {
    return {}
  }
}

function writeTeamOpenFormDraft(draft: TeamOpenFormDraft) {
  try {
    window.localStorage.setItem(TEAM_OPEN_FORM_DRAFT_STORAGE_KEY, JSON.stringify(draft))
  } catch {
    // localStorage may be unavailable in private/locked-down browser contexts.
  }
}

const defaultConfig: TeamOpenConfig = {
  team_open_precheck_enabled: true,
  team_open_precheck_attempts: 2,
  team_open_auto_submit_payment: true,
  team_open_reuse_cards: false,
  team_open_use_proxy: true,
  team_open_proxy: '',
  team_open_payment_service_base_url: 'https://team.aimizy.com',
  team_open_payment_service_plan_name: 'chatgptteamplan',
  team_open_payment_service_country: 'SG',
  team_open_payment_service_currency: 'SGD',
  team_open_payment_service_promo_campaign_id: 'team-1-month-free',
  team_open_payment_service_price_interval: 'month',
  team_open_payment_service_seat_quantity: 5,
  team_open_payment_service_check_card_proxy: false,
  team_open_payment_service_is_short_link: false,
  team_open_pro_checkout_country: 'US',
  team_open_pro_checkout_currency: 'USD',
  team_open_default_holder_name: 'lu',
  team_open_default_billing_email: 'redwood@mail.yaoke.vip',
  team_open_default_country: 'US',
  team_open_default_state: 'CA',
  team_open_default_city: 'Woodland',
  team_open_default_line1: '120 Main Street',
  team_open_default_postal_code: '95695',
  team_open_post_payment_wait_seconds: 30,
  team_open_verification_retries: 3,
}

export default function TeamOpenPage() {
  const initialDraft = useMemo(() => readTeamOpenFormDraft(), [])
  const [config, setConfig] = useState<TeamOpenConfig>({ ...defaultConfig, ...(initialDraft.config || {}) })
  const [count, setCount] = useState(() => clampNumber(initialDraft.count, 1, 1, 500))
  const [concurrency, setConcurrency] = useState(() => clampNumber(initialDraft.concurrency, 1, 1, 20))
  const [executorType, setExecutorType] = useState<Executor>(initialDraft.executorType || 'protocol')
  const [saving, setSaving] = useState(false)
  const [starting, setStarting] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [importingCards, setImportingCards] = useState(false)
  const [deletingCardId, setDeletingCardId] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [activeJobId, setActiveJobId] = useState('')
  const [snapshot, setSnapshot] = useState<TeamOpenJobSnapshot | null>(null)
  const [jobHistory, setJobHistory] = useState<TeamOpenJobListItem[]>([])
  const [cardSummary, setCardSummary] = useState<TeamOpenCardSummary | null>(null)
  const [cardImportText, setCardImportText] = useState('')
  const [cardImportResult, setCardImportResult] = useState<{ success: number; updated: number; failed: number; errors: string[] } | null>(null)
  const [draftReady, setDraftReady] = useState(false)
  const eventLogRef = useRef<HTMLDivElement | null>(null)

  const isRunning = useMemo(() => ['pending', 'running'].includes(snapshot?.status || ''), [snapshot])
  const resultItems = useMemo(() => snapshot?.results || [], [snapshot?.results])
  const eventItems = useMemo(() => snapshot?.events || [], [snapshot?.events])

  function updateConfig<K extends keyof TeamOpenConfig>(key: K, value: TeamOpenConfig[K]) {
    setConfig((prev) => ({ ...prev, [key]: value }))
  }

  async function refreshConfig() {
    const detail = await apiFetch<Partial<TeamOpenConfig>>('/api/team-open/config')
    const draft = readTeamOpenFormDraft()
    setConfig((prev) => ({ ...prev, ...detail, ...(draft.config || {}) }))
  }

  async function refreshCards() {
    const detail = await apiFetch<TeamOpenCardSummary>('/api/team-open/cards')
    setCardSummary(detail)
  }

  async function refreshJobs() {
    const detail = await apiFetch<{ items: TeamOpenJobListItem[] }>('/api/team-open/jobs?limit=20')
    const items = detail.items || []
    setJobHistory(items)
    if (!activeJobId && items.length > 0) {
      const latestJobId = String(items[0]?.id || '')
      if (latestJobId) {
        setActiveJobId(latestJobId)
        window.localStorage.setItem(ACTIVE_TEAM_OPEN_JOB_STORAGE_KEY, latestJobId)
      }
    }
  }

  async function refreshJob(jobId: string) {
    if (!jobId) return
    const detail = await apiFetch<TeamOpenJobSnapshot>(`/api/team-open/jobs/${jobId}`)
    setSnapshot(detail)
  }

  useEffect(() => {
    if (!draftReady) return
    writeTeamOpenFormDraft({ count, concurrency, executorType, config })
  }, [draftReady, count, concurrency, executorType, config])

  useEffect(() => {
    const cachedJobId = window.localStorage.getItem(ACTIVE_TEAM_OPEN_JOB_STORAGE_KEY) || ''
    if (cachedJobId) {
      setActiveJobId(cachedJobId)
    }
    void refreshConfig()
      .catch((err) => setError(err instanceof Error ? err.message : '加载 Team 开通配置失败'))
      .finally(() => setDraftReady(true))
    void refreshCards()
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

  useEffect(() => {
    const element = eventLogRef.current
    if (!element) return
    element.scrollTop = element.scrollHeight
  }, [activeJobId, eventItems.length])

  async function saveDefaults() {
    setSaving(true)
    setError('')
    try {
      await apiFetch('/api/team-open/config', {
        method: 'PUT',
        body: JSON.stringify({ values: config }),
      })
      await refreshConfig()
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存 Team 开通配置失败')
    } finally {
      setSaving(false)
    }
  }

  async function importCards() {
    if (!cardImportText.trim()) {
      setError('请先粘贴银行卡列表')
      return
    }
    setImportingCards(true)
    setError('')
    try {
      const result = await apiFetch<{ success: number; updated: number; failed: number; errors: string[]; summary: TeamOpenCardSummary; imported_card?: Partial<TeamOpenCardItem> }>('/api/team-open/cards/import', {
        method: 'POST',
        body: JSON.stringify({
          data: cardImportText,
          enabled: true,
          default_holder_name: config.team_open_default_holder_name,
          default_billing_email: config.team_open_default_billing_email,
          default_country: config.team_open_default_country,
          default_state: config.team_open_default_state,
          default_city: config.team_open_default_city,
          default_line1: config.team_open_default_line1,
          default_postal_code: config.team_open_default_postal_code,
        }),
      })
      setCardImportResult({ success: result.success, updated: result.updated, failed: result.failed, errors: result.errors || [] })
      setCardSummary(result.summary)
      const importedCard = result.imported_card || {}
      setConfig((prev) => ({
        ...prev,
        team_open_default_country: importedCard.country || prev.team_open_default_country,
        team_open_default_state: importedCard.state || prev.team_open_default_state,
        team_open_default_city: importedCard.city || prev.team_open_default_city,
        team_open_default_line1: importedCard.line1 || prev.team_open_default_line1,
        team_open_default_postal_code: importedCard.postal_code || prev.team_open_default_postal_code,
      }))
      setCardImportText('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '导入银行卡失败')
    } finally {
      setImportingCards(false)
    }
  }

  async function deleteCard(cardId: number) {
    setDeletingCardId(cardId)
    setError('')
    try {
      const result = await apiFetch<{ ok: boolean; summary: TeamOpenCardSummary }>(`/api/team-open/cards/${cardId}`, { method: 'DELETE' })
      setCardSummary(result.summary)
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除银行卡失败')
    } finally {
      setDeletingCardId(null)
    }
  }

  async function startJob(event: FormEvent) {
    event.preventDefault()
    setStarting(true)
    setError('')
    try {
      const response = await apiFetch<{ job_id: string }>('/api/team-open/jobs', {
        method: 'POST',
        body: JSON.stringify({
          count,
          concurrency,
          executor_type: executorType,
          options: config,
        }),
      })
      setActiveJobId(response.job_id)
      window.localStorage.setItem(ACTIVE_TEAM_OPEN_JOB_STORAGE_KEY, response.job_id)
      await refreshJobs()
      await refreshJob(response.job_id)
      await refreshCards()
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建 Team 母号开通任务失败')
    } finally {
      setStarting(false)
    }
  }

  async function stopJob() {
    if (!activeJobId) return
    setStopping(true)
    setError('')
    try {
      await apiFetch(`/api/team-open/jobs/${activeJobId}/stop`, { method: 'POST' })
      await refreshJobs()
      await refreshJob(activeJobId)
    } catch (err) {
      setError(err instanceof Error ? err.message : '停止任务失败')
    } finally {
      setStopping(false)
    }
  }

  return (
    <div className="codex-auth-batch-layout">
      {error ? <div className="error-banner">{error}</div> : null}

      <main className="workbench-grid two-pane codex-auth-batch-grid">
        <section className="panel form-panel">
          <div className="panel-title-row">
            <div>
              <h2>Team 母号批量开通</h2>
              <span className="hint">流程：Cloud Mail 自动注册新账号作为母号 → Pro 预检两次 → 生成 Team 支付链接 → 自动绑卡 → 校验组织工作区。</span>
            </div>
          </div>

          <form className="config-form" onSubmit={startJob}>
            <div className="sub-block">
              <div className="sub-block-title">任务参数</div>
              <div className="field-group two-col compact">
                <label>
                  <span>开通数量</span>
                  <input type="number" min={1} max={500} value={count} onChange={(e) => setCount(Number(e.target.value || 1))} />
                </label>
                <label>
                  <span>并发数</span>
                  <input type="number" min={1} max={20} value={concurrency} onChange={(e) => setConcurrency(Number(e.target.value || 1))} />
                </label>
              </div>
              <label>
                <span>执行器</span>
                <select value={executorType} onChange={(e) => setExecutorType(e.target.value as Executor)}>
                  <option value="protocol">protocol</option>
                  <option value="headless">headless</option>
                  <option value="headed">headed</option>
                </select>
              </label>
            </div>

            <div className="sub-block">
              <div className="sub-block-title">流程选项</div>
              <label className="checkbox-row settings-checkbox-row">
                <input type="checkbox" checked={config.team_open_precheck_enabled} onChange={(e) => updateConfig('team_open_precheck_enabled', e.target.checked)} />
                <span>先执行 Pro 预检拒付</span>
              </label>
              <label className="checkbox-row settings-checkbox-row">
                <input type="checkbox" checked={config.team_open_auto_submit_payment} onChange={(e) => updateConfig('team_open_auto_submit_payment', e.target.checked)} />
                <span>自动提交 Team 绑卡</span>
              </label>
              <label className="checkbox-row settings-checkbox-row">
                <input type="checkbox" checked={config.team_open_reuse_cards} onChange={(e) => updateConfig('team_open_reuse_cards', e.target.checked)} />
                <span>允许重复使用银行卡</span>
              </label>
              <label className="checkbox-row settings-checkbox-row">
                <input type="checkbox" checked={config.team_open_use_proxy} onChange={(e) => updateConfig('team_open_use_proxy', e.target.checked)} />
                <span>启用代理（为空时自动从代理池取）</span>
              </label>
              <div className="field-group two-col compact">
                <label>
                  <span>Pro 预检次数</span>
                  <input type="number" min={1} max={5} value={config.team_open_precheck_attempts} onChange={(e) => updateConfig('team_open_precheck_attempts', Number(e.target.value || 1))} />
                </label>
                <label>
                  <span>固定代理（可选）</span>
                  <input value={config.team_open_proxy} onChange={(e) => updateConfig('team_open_proxy', e.target.value)} placeholder="http://user:pass@host:port" />
                </label>
              </div>
              <div className="field-group two-col compact">
                <label>
                  <span>支付后等待秒数</span>
                  <input type="number" min={0} max={300} value={config.team_open_post_payment_wait_seconds} onChange={(e) => updateConfig('team_open_post_payment_wait_seconds', Number(e.target.value || 0))} />
                </label>
                <label>
                  <span>验证重试次数</span>
                  <input type="number" min={1} max={10} value={config.team_open_verification_retries} onChange={(e) => updateConfig('team_open_verification_retries', Number(e.target.value || 1))} />
                </label>
              </div>
            </div>

            <div className="sub-block">
              <div className="sub-block-title">支付链接服务</div>
              <label>
                <span>服务地址</span>
                <input value={config.team_open_payment_service_base_url} onChange={(e) => updateConfig('team_open_payment_service_base_url', e.target.value)} placeholder="https://team.aimizy.com" />
              </label>
              <div className="field-group two-col compact">
                <label>
                  <span>Plan Name</span>
                  <input value={config.team_open_payment_service_plan_name} onChange={(e) => updateConfig('team_open_payment_service_plan_name', e.target.value)} />
                </label>
                <label>
                  <span>Promo Campaign</span>
                  <input value={config.team_open_payment_service_promo_campaign_id} onChange={(e) => updateConfig('team_open_payment_service_promo_campaign_id', e.target.value)} />
                </label>
              </div>
              <div className="field-group two-col compact">
                <label>
                  <span>国家</span>
                  <input value={config.team_open_payment_service_country} onChange={(e) => updateConfig('team_open_payment_service_country', e.target.value.toUpperCase())} />
                </label>
                <label>
                  <span>币种</span>
                  <input value={config.team_open_payment_service_currency} onChange={(e) => updateConfig('team_open_payment_service_currency', e.target.value.toUpperCase())} />
                </label>
              </div>
              <div className="field-group two-col compact">
                <label>
                  <span>计费周期</span>
                  <input value={config.team_open_payment_service_price_interval} onChange={(e) => updateConfig('team_open_payment_service_price_interval', e.target.value)} />
                </label>
                <label>
                  <span>席位数</span>
                  <input type="number" min={1} max={50} value={config.team_open_payment_service_seat_quantity} onChange={(e) => updateConfig('team_open_payment_service_seat_quantity', Number(e.target.value || 1))} />
                </label>
              </div>
            </div>

            <div className="sub-block">
              <div className="sub-block-title">绑卡默认信息</div>
              <div className="field-group two-col compact">
                <label>
                  <span>默认持卡人</span>
                  <input value={config.team_open_default_holder_name} onChange={(e) => updateConfig('team_open_default_holder_name', e.target.value)} placeholder="James Kvale" />
                </label>
                <label>
                  <span>默认账单邮箱</span>
                  <input value={config.team_open_default_billing_email} onChange={(e) => updateConfig('team_open_default_billing_email', e.target.value)} placeholder="lu@gmail.com" />
                </label>
              </div>
              <div className="field-group two-col compact">
                <label>
                  <span>国家</span>
                  <input value={config.team_open_default_country} onChange={(e) => updateConfig('team_open_default_country', e.target.value)} placeholder="US / United States" />
                </label>
                <label>
                  <span>州</span>
                  <input value={config.team_open_default_state} onChange={(e) => updateConfig('team_open_default_state', e.target.value)} placeholder="CO" />
                </label>
              </div>
              <div className="field-group two-col compact">
                <label>
                  <span>城市</span>
                  <input value={config.team_open_default_city} onChange={(e) => updateConfig('team_open_default_city', e.target.value)} placeholder="Almont" />
                </label>
                <label>
                  <span>邮编</span>
                  <input value={config.team_open_default_postal_code} onChange={(e) => updateConfig('team_open_default_postal_code', e.target.value)} placeholder="81210" />
                </label>
              </div>
              <label>
                <span>地址</span>
                <input value={config.team_open_default_line1} onChange={(e) => updateConfig('team_open_default_line1', e.target.value)} placeholder="120 Main Street" />
              </label>
              <div className="field-group two-col compact">
                <label>
                  <span>Pro 预检国家</span>
                  <input value={config.team_open_pro_checkout_country} onChange={(e) => updateConfig('team_open_pro_checkout_country', e.target.value.toUpperCase())} />
                </label>
                <label>
                  <span>Pro 预检币种</span>
                  <input value={config.team_open_pro_checkout_currency} onChange={(e) => updateConfig('team_open_pro_checkout_currency', e.target.value.toUpperCase())} />
                </label>
              </div>
            </div>

            <div className="form-actions split-actions">
              <button className="ghost-btn" type="button" onClick={() => void saveDefaults()} disabled={saving}>
                {saving ? '保存中...' : '保存默认配置'}
              </button>
              {isRunning ? (
                <button className="ghost-btn danger" type="button" onClick={() => void stopJob()} disabled={stopping || !activeJobId}>
                  {stopping ? '停止中...' : '停止任务'}
                </button>
              ) : null}
              <button className="primary-btn" type="submit" disabled={starting}>
                {starting ? '任务创建中...' : '开始批量开通'}
              </button>
            </div>
          </form>

          <div className="sub-block">
            <div className="sub-block-title">银行卡导入</div>
            <label>
              <span>导入格式</span>
              <textarea
                rows={8}
                value={cardImportText}
                onChange={(e) => setCardImportText(e.target.value)}
                placeholder={'示例：\n5349336300545701 0431 881\n5349336394800665 04/32 007\n\n地址、姓名、国家、州、城市、邮编统一使用上方默认信息'}
              />
            </label>
            <div className="form-actions">
              <button className="primary-btn" type="button" onClick={() => void importCards()} disabled={importingCards}>
                {importingCards ? '导入中...' : '导入银行卡'}
              </button>
            </div>
            {cardImportResult ? (
              <div className="import-result">
                <div className="import-result-head">
                  <strong>最近一次导入</strong>
                  <span>新增 {cardImportResult.success} / 更新 {cardImportResult.updated} / 失败 {cardImportResult.failed}</span>
                </div>
                {cardImportResult.errors.length > 0 ? (
                  <div className="import-error-list">
                    {cardImportResult.errors.slice(0, 5).map((item, index) => (
                      <div className="import-error-item" key={`${item}-${index}`}>{item}</div>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        </section>

        <section className="panel account-panel">
          <div className="panel-title-row">
            <div>
              <h2>任务 / 银行卡概览</h2>
              <span className="hint">成功后会自动尝试导入 Codex Team 母号池。</span>
            </div>
            <select
              className="page-size-select"
              value={activeJobId}
              onChange={(e) => {
                setActiveJobId(e.target.value)
                window.localStorage.setItem(ACTIVE_TEAM_OPEN_JOB_STORAGE_KEY, e.target.value)
              }}
            >
              {jobHistory.length === 0 ? <option value="">-</option> : null}
              {jobHistory.map((item) => (
                <option value={item.id} key={item.id}>{item.id} · {item.progress} · {item.status}</option>
              ))}
            </select>
          </div>

          <div className="mini-stats-grid">
            <div className="stat-card tone-default"><span>银行卡总数</span><strong>{cardSummary?.total ?? 0}</strong></div>
            <div className="stat-card tone-success"><span>启用</span><strong>{cardSummary?.enabled ?? 0}</strong></div>
            <div className="stat-card tone-info"><span>成功次数</span><strong>{cardSummary?.success_count ?? 0}</strong></div>
            <div className="stat-card tone-danger"><span>失败次数</span><strong>{cardSummary?.failure_count ?? 0}</strong></div>
          </div>

          <div className="sub-block">
            <div className="sub-block-title">银行卡列表</div>
            <div className="provider-item-list">
              {(cardSummary?.items || []).length === 0 ? <div className="timeline-empty">暂无银行卡</div> : null}
              {(cardSummary?.items || []).map((item) => (
                <div className="provider-item provider-item-compact" key={item.id}>
                  <div className="provider-item-main">
                    <strong>{item.label || item.masked_number}</strong>
                    <p>{item.masked_number} · {item.exp_month}/{item.exp_year} · {item.country || '-'} {item.city || ''}</p>
                    <p>{item.holder_name || '-'} · {item.billing_email || '-'}</p>
                    {item.last_error ? <p className="danger-text">{item.last_error}</p> : null}
                  </div>
                  <div className="provider-item-meta">
                    <span className="proxy-count proxy-count-success">{item.success_count}</span>
                    <span className="proxy-count proxy-count-failed">{item.failure_count}</span>
                    <button className="tiny-action-btn danger" type="button" onClick={() => void deleteCard(item.id)} disabled={deletingCardId === item.id}>
                      {deletingCardId === item.id ? '删除中' : '删除'}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="sub-block">
            <div className="sub-block-title">任务结果</div>
            {!snapshot ? <div className="timeline-empty">暂无任务</div> : null}
            {snapshot ? (
              <>
                <div className="mini-stats-grid">
                  <div className="stat-card tone-default"><span>状态</span><strong>{snapshot.status}</strong></div>
                  <div className="stat-card tone-default"><span>进度</span><strong>{snapshot.progress}</strong></div>
                  <div className="stat-card tone-success"><span>成功</span><strong>{snapshot.success}</strong></div>
                  <div className="stat-card tone-danger"><span>失败</span><strong>{snapshot.failed}</strong></div>
                </div>

                <div className="provider-item-list">
                  {resultItems.length === 0 ? <div className="timeline-empty">暂无结果</div> : null}
                  {resultItems.map((item) => (
                    <div className="provider-item" key={`${item.attempt_index}-${item.email || item.created_at}`}>
                      <div className="provider-item-main">
                        <strong>#{item.attempt_index} · {item.email || '-'}</strong>
                        <p>{item.status} · {item.card_label || '-'}</p>
                        <p>预检：{item.precheck_status || '-'} · 绑卡：{item.payment_status || '-'} · 母号池ID：{item.imported_parent_id || '-'}</p>
                        {item.team_account_id ? <p>Team Workspace：{item.team_account_id} {item.team_name ? `· ${item.team_name}` : ''}</p> : null}
                        {item.payment_url ? (
                          <p><a href={item.payment_url} target="_blank" rel="noreferrer">打开支付链接</a></p>
                        ) : null}
                        {item.error ? <p className="danger-text">{item.error}</p> : null}
                      </div>
                      <div className="provider-item-meta">
                        <span>{formatTime(item.updated_at)}</span>
                      </div>
                    </div>
                  ))}
                </div>

                <div className="sub-block-title" style={{ marginTop: 12 }}>事件日志</div>
                <div className="timeline-list team-open-log-list" ref={eventLogRef}>
                  {eventItems.length === 0 ? <div className="timeline-empty">暂无日志</div> : null}
                  {eventItems.slice(-120).map((item) => (
                    <div className="account-log-line" key={item.id}>
                      {`${formatTime(item.created_at)}${item.account_email && item.account_email !== '-' ? ` [${item.account_email}]` : ''}  ${item.message}`.trim()}
                    </div>
                  ))}
                </div>
              </>
            ) : null}
          </div>
        </section>
      </main>
    </div>
  )
}
