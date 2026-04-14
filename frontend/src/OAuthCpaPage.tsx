import { useEffect, useMemo, useState } from 'react'
import { apiFetch, formatTime, maskToken } from './lib/api'

type OAuthSession = {
  auth_url: string
  state: string
  code_verifier: string
  redirect_uri: string
}

type OAuthExchangeResult = {
  email: string
  account_id: string
  oauth_account: Record<string, unknown>
  cpa_account: Record<string, unknown>
  cpa_json: string
}

type ParsedAccountItem = {
  key: string
  email: string
  account_id: string
  oauth_account: Record<string, unknown>
  cpa_account: Record<string, unknown>
  cpa_json: string
  created_at: number
}

const OAUTH_CPA_SESSION_STORAGE_KEY = 'oauth_cpa_link_session'
const OAUTH_CPA_HISTORY_STORAGE_KEY = 'oauth_cpa_success_accounts'

export default function OAuthCpaPage() {
  const [session, setSession] = useState<OAuthSession | null>(null)
  const [callbackUrl, setCallbackUrl] = useState('')
  const [result, setResult] = useState<OAuthExchangeResult | null>(null)
  const [historyItems, setHistoryItems] = useState<ParsedAccountItem[]>([])
  const [error, setError] = useState('')
  const [generating, setGenerating] = useState(false)
  const [exchanging, setExchanging] = useState(false)
  const [copyState, setCopyState] = useState('')

  useEffect(() => {
    const raw = window.localStorage.getItem(OAUTH_CPA_SESSION_STORAGE_KEY) || ''
    if (!raw) return
    try {
      const parsed = JSON.parse(raw) as OAuthSession
      if (parsed?.auth_url && parsed?.state && parsed?.code_verifier) {
        setSession(parsed)
      }
    } catch {
      window.localStorage.removeItem(OAUTH_CPA_SESSION_STORAGE_KEY)
    }
  }, [])

  useEffect(() => {
    const raw = window.localStorage.getItem(OAUTH_CPA_HISTORY_STORAGE_KEY) || ''
    if (!raw) return
    try {
      const parsed = JSON.parse(raw) as ParsedAccountItem[]
      if (Array.isArray(parsed)) {
        setHistoryItems(parsed.filter((item) => !!item?.key && !!item?.cpa_json))
      }
    } catch {
      window.localStorage.removeItem(OAUTH_CPA_HISTORY_STORAGE_KEY)
    }
  }, [])

  useEffect(() => {
    if (!session) {
      window.localStorage.removeItem(OAUTH_CPA_SESSION_STORAGE_KEY)
      return
    }
    window.localStorage.setItem(OAUTH_CPA_SESSION_STORAGE_KEY, JSON.stringify(session))
  }, [session])

  useEffect(() => {
    window.localStorage.setItem(OAUTH_CPA_HISTORY_STORAGE_KEY, JSON.stringify(historyItems))
  }, [historyItems])

  useEffect(() => {
    if (!copyState) return
    const timer = window.setTimeout(() => setCopyState(''), 1600)
    return () => window.clearTimeout(timer)
  }, [copyState])

  const oauthAccount = useMemo(() => result?.oauth_account || {}, [result?.oauth_account])
  const historyExportJson = useMemo(
    () => JSON.stringify(historyItems.map((item) => item.cpa_account), null, 2),
    [historyItems],
  )
  const historyExportJsonl = useMemo(
    () => historyItems.map((item) => JSON.stringify(item.cpa_account)).join('\n'),
    [historyItems],
  )

  async function createLink() {
    setGenerating(true)
    setError('')
    try {
      const detail = await apiFetch<OAuthSession>('/api/oauth-cpa/link')
      setSession(detail)
      setResult(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '生成授权链接失败')
    } finally {
      setGenerating(false)
    }
  }

  async function handleExchange() {
    if (!session?.state || !session.code_verifier) {
      setError('请先生成授权链接')
      return
    }
    if (!callbackUrl.trim()) {
      setError('请粘贴回调链接')
      return
    }
    setExchanging(true)
    setError('')
    try {
      const detail = await apiFetch<OAuthExchangeResult>('/api/oauth-cpa/exchange', {
        method: 'POST',
        body: JSON.stringify({
          callback_url: callbackUrl,
          state: session.state,
          code_verifier: session.code_verifier,
        }),
      })
      setResult(detail)
      appendHistory(detail)
    } catch (err) {
      setError(err instanceof Error ? err.message : '处理回调失败')
    } finally {
      setExchanging(false)
    }
  }

  function appendHistory(detail: OAuthExchangeResult) {
    const email = String(detail.email || '')
    const accountId = String(detail.account_id || '')
    const key = `${accountId || email}:${email || accountId}`
    const nextItem: ParsedAccountItem = {
      key,
      email,
      account_id: accountId,
      oauth_account: detail.oauth_account || {},
      cpa_account: detail.cpa_account || {},
      cpa_json: detail.cpa_json || '',
      created_at: Date.now() / 1000,
    }
    setHistoryItems((prev) => [nextItem, ...prev.filter((item) => item.key !== key)])
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

  function loadHistoryItem(item: ParsedAccountItem) {
    setResult({
      email: item.email,
      account_id: item.account_id,
      oauth_account: item.oauth_account,
      cpa_account: item.cpa_account,
      cpa_json: item.cpa_json,
    })
  }

  function removeHistoryItem(key: string) {
    setHistoryItems((prev) => prev.filter((item) => item.key !== key))
    if (result && `${result.account_id || result.email}:${result.email || result.account_id}` === key) {
      setResult(null)
    }
  }

  function clearHistory() {
    setHistoryItems([])
  }

  return (
    <div className="oauth-cpa-layout">
      {error ? <div className="error-banner">{error}</div> : null}

      <main className="workbench-grid two-pane oauth-cpa-grid">
        <section className="panel form-panel">
          <div className="panel-title-row">
            <div>
              <h2>OAuth 转 CPA</h2>
              <span className="hint">生成授权链接，浏览器完成授权后粘贴回调链接，直接拿到 CPA 格式账号。</span>
            </div>
          </div>

          <div className="config-form">
            <div className="sub-block">
              <div className="sub-block-title">步骤 1：生成授权链接</div>
              <div className="helper-note">
                <strong>说明</strong>
                <p>点击生成后，复制链接到浏览器打开，完成 OpenAI 授权。</p>
              </div>
              <div className="form-actions split-actions">
                <button className="primary-btn" type="button" onClick={() => void createLink()} disabled={generating}>
                  {generating ? '生成中...' : '生成 OAuth 链接'}
                </button>
              </div>
              <label>
                <span>授权链接</span>
                <textarea rows={6} value={session?.auth_url || ''} readOnly placeholder="点击上方按钮后生成" />
              </label>
              <div className="oauth-inline-actions">
                <button className="ghost-btn" type="button" onClick={() => void copyText(session?.auth_url || '', '已复制授权链接')} disabled={!session?.auth_url}>
                  复制链接
                </button>
                <button className="ghost-btn" type="button" onClick={() => window.open(session?.auth_url || '', '_blank', 'noopener,noreferrer')} disabled={!session?.auth_url}>
                  浏览器打开
                </button>
              </div>
              <div className="helper-note oauth-meta-note">
                <strong>回调地址</strong>
                <p>{session?.redirect_uri || 'http://localhost:1455/auth/callback'}</p>
              </div>
            </div>

            <div className="sub-block">
              <div className="sub-block-title">步骤 2：粘贴回调链接</div>
              <label>
                <span>回调链接</span>
                <textarea
                  rows={6}
                  value={callbackUrl}
                  onChange={(e) => setCallbackUrl(e.target.value)}
                  placeholder="把浏览器回跳后的完整链接粘贴到这里"
                />
              </label>
              <div className="form-actions split-actions">
                <button className="primary-btn" type="button" onClick={() => void handleExchange()} disabled={exchanging || !session}>
                  {exchanging ? '处理中...' : '解析回调并生成 CPA'}
                </button>
              </div>
            </div>
          </div>
        </section>

        <section className="panel account-panel oauth-cpa-result-panel">
          <div className="panel-title-row">
            <div>
              <h2>结果</h2>
              <span className="hint">拿到结果后可直接复制 CPA JSON。</span>
            </div>
            {copyState ? <span className="status-chip">{copyState}</span> : null}
          </div>

          <div className="codex-stats-grid">
            <div className="codex-stat tone-default">
              <span>邮箱</span>
              <strong>{result?.email || '-'}</strong>
            </div>
            <div className="codex-stat tone-default">
              <span>Account ID</span>
              <strong>{result?.account_id || '-'}</strong>
            </div>
          </div>

          <div className="sub-block">
            <div className="sub-block-title">OAuth 信息</div>
            <div className="provider-item-list oauth-summary-list">
              <div className="provider-item">
                <div>
                  <strong>access_token</strong>
                  <p>{maskToken(String(oauthAccount.access_token || ''))}</p>
                </div>
              </div>
              <div className="provider-item">
                <div>
                  <strong>refresh_token</strong>
                  <p>{maskToken(String(oauthAccount.refresh_token || ''))}</p>
                </div>
              </div>
              <div className="provider-item">
                <div>
                  <strong>expired</strong>
                  <p>{String(oauthAccount.expired || '-')}</p>
                </div>
              </div>
            </div>
          </div>

          <div className="sub-block">
            <div className="panel-title-row oauth-result-title">
              <div className="sub-block-title">CPA JSON</div>
              <button className="ghost-btn" type="button" onClick={() => void copyText(result?.cpa_json || '', '已复制 CPA JSON')} disabled={!result?.cpa_json}>
                复制 CPA
              </button>
            </div>
            <textarea className="oauth-result-textarea" rows={18} value={result?.cpa_json || ''} readOnly placeholder="完成回调解析后，这里会输出 CPA 格式账号 JSON" />
          </div>

          <div className="sub-block">
            <div className="panel-title-row oauth-result-title">
              <div>
                <div className="sub-block-title">解析成功账号列表</div>
                <span className="hint">已成功解析 {historyItems.length} 个账号，刷新后仍会保留在当前浏览器。</span>
              </div>
              <button className="ghost-btn danger oauth-mini-btn" type="button" onClick={() => clearHistory()} disabled={historyItems.length === 0}>
                清空列表
              </button>
            </div>

            <div className="oauth-inline-actions oauth-export-actions">
              <button className="ghost-btn" type="button" onClick={() => void copyText(historyExportJson, '已复制批量 JSON')} disabled={historyItems.length === 0}>
                批量复制 JSON
              </button>
              <button className="ghost-btn" type="button" onClick={() => void copyText(historyExportJsonl, '已复制 JSONL')} disabled={historyItems.length === 0}>
                批量复制 JSONL
              </button>
              <button className="ghost-btn" type="button" onClick={() => downloadText(`oauth_cpa_export_${Date.now()}.json`, historyExportJson)} disabled={historyItems.length === 0}>
                导出 JSON
              </button>
              <button className="ghost-btn" type="button" onClick={() => downloadText(`oauth_cpa_export_${Date.now()}.jsonl`, historyExportJsonl, 'application/x-ndjson;charset=utf-8')} disabled={historyItems.length === 0}>
                导出 JSONL
              </button>
            </div>

            <div className="provider-item-list oauth-history-list">
              {historyItems.length === 0 ? <div className="timeline-empty">暂无成功账号</div> : null}
              {historyItems.map((item) => (
                <div className="provider-item oauth-history-item" key={item.key}>
                  <div>
                    <strong>{item.email || '-'}</strong>
                    <p>{item.account_id || '-'}</p>
                    <p>{formatTime(item.created_at)}</p>
                  </div>
                  <div className="provider-item-meta oauth-history-actions">
                    <button className="ghost-btn oauth-mini-btn" type="button" onClick={() => loadHistoryItem(item)}>
                      查看
                    </button>
                    <button className="ghost-btn oauth-mini-btn" type="button" onClick={() => void copyText(item.cpa_json, '已复制单条 CPA')}>
                      复制
                    </button>
                    <button className="ghost-btn danger oauth-mini-btn" type="button" onClick={() => removeHistoryItem(item.key)}>
                      删除
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </section>
      </main>
    </div>
  )
}
