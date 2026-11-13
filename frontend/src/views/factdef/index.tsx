import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/factdef'
const columns = [
  '事实编号',
  '事实名称',
  '源表名称',
  '粒度说明',
  '度量字段',
  '分区字段',
  '口径版本',
  '受影响窗口',
  '下游重算状态',
]
const actions = ['提交审批', '发布事实', '废弃事实']
const statuses = ['草稿', '待审批', '已生效', '已废弃']

type Row = Record<string, any>

type Plan = {
  '流水编号': string
  '输出表名': string
  '汇总粒度': string
  '时间窗口': string
  '受影响窗口': string[]
  '待处理分区': string[]
  '重算状态': string
  '可消费版本': string | null
}

export default function FactdefView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [editing, setEditing] = useState<Row | null>(null)
  const [form, setForm] = useState({ 度量字段: '', 分区字段: '', 口径生效时间: '', 备注: '' })
  const [downstream, setDownstream] = useState<Plan[]>([])
  const filterFields = useMemo(() => ['事实编号', '事实名称', '源表名称'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('事实定义列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '事实表管理列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const stats = useMemo(() => [
    { label: '事实定义总数', value: total },
    { label: '已生效事实', value: rows.filter((row) => row.status === '已生效').length },
    { label: '下游待重算', value: rows.filter((row) => row['待重算']).length },
  ], [rows, total])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message ?? '事实表管理动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '事实表管理操作失败')
    }
  }

  function openEdit(row: Row) {
    setEditing(row)
    setDownstream([])
    setNotice('')
    setErrorMessage('')
    setForm({
      度量字段: String(row['度量字段'] ?? ''),
      分区字段: String(row['分区字段'] ?? ''),
      口径生效时间: '2026-09-03 00:00:00',
      备注: '根据业务口径调整度量字段',
    })
  }
  async function submitEdit() {
    if (!editing) return
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${editing.id}/metrics`, {
        method: 'POST',
        body: JSON.stringify({ values: form }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message ?? '度量口径修改失败')
      }
      setNotice(payload.message)
      setDownstream(payload.entry?.['下游聚合流水'] ?? [])
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '度量口径修改失败')
    }
  }

  return (
    <section className="page" data-module="factdef">
      <header className="page-head">
        <div>
          <h2>事实表管理</h2>
          <p className="page-desc">维护事实表的粒度定义、维度关联与度量清单。发布后修改度量会登记新的口径版本，并给出下游聚合流水受影响的窗口。</p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出事实表管理清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      {(notice || errorMessage) && (
        <div className="notice-bar">
          {notice && <span className="notice-text">{notice}</span>}
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </div>
      )}

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={row['待重算'] ? 'row-warn' : undefined}>
              {columns.map((column) => (
                <td key={column}>
                  {column === '下游重算状态' && row[column]
                    ? String(row[column]).split(',').join('、')
                    : (row[column] ?? '—')}
                </td>
              ))}
              <td>{row.status}</td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button
                  className="link"
                  type="button"
                  disabled={row.status !== '已生效'}
                  onClick={() => openEdit(row)}
                >
                  修改度量口径
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无事实表管理数据，可先登记事实定义</td>
            </tr>
          )}
        </tbody>
      </table>

      {editing && (
        <article className="detail-panel">
          <header className="detail-head">
            <h3>修改度量口径 · {editing['事实编号']} / {editing['事实名称']}</h3>
            <button className="btn ghost" type="button" onClick={() => setEditing(null)}>关闭</button>
          </header>
          <div className="detail-form">
            <label className="filter-item">
              <span>度量字段（逗号分隔）</span>
              <input value={form.度量字段} onChange={(event) => setForm((prev) => ({ ...prev, 度量字段: event.target.value }))} />
            </label>
            <label className="filter-item">
              <span>分区字段</span>
              <input value={form.分区字段} onChange={(event) => setForm((prev) => ({ ...prev, 分区字段: event.target.value }))} />
            </label>
            <label className="filter-item">
              <span>口径生效时间</span>
              <input value={form.口径生效时间} onChange={(event) => setForm((prev) => ({ ...prev, 口径生效时间: event.target.value }))} />
            </label>
            <label className="filter-item">
              <span>备注</span>
              <input value={form.备注} onChange={(event) => setForm((prev) => ({ ...prev, 备注: event.target.value }))} />
            </label>
            <button className="btn primary" type="button" onClick={() => void submitEdit()}>登记新口径版本</button>
          </div>
          {downstream.length > 0 && (
            <>
              <h4 className="detail-title">受影响的下游聚合流水</h4>
              <table className="data-table compact">
                <thead><tr><th>流水编号</th><th>输出表名</th><th>汇总粒度</th><th>时间窗口</th><th>受影响窗口</th><th>待处理分区</th><th>重算状态</th><th>可消费版本</th></tr></thead>
                <tbody>
                  {downstream.map((plan) => (
                    <tr key={plan['流水编号']}>
                      <td>{plan['流水编号']}</td>
                      <td>{plan['输出表名']}</td>
                      <td>{plan['汇总粒度']}</td>
                      <td>{plan['时间窗口']}</td>
                      <td>{plan['受影响窗口']?.join('、') || '—'}</td>
                      <td>{plan['待处理分区']?.join('、') || '—'}</td>
                      <td>{plan['重算状态']}</td>
                      <td>{plan['可消费版本'] ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </article>
      )}

      <footer className="page-foot">
        <span>共 {total} 条事实表管理记录</span>
        <span className="page-desc">{statuses.join(' / ')}</span>
      </footer>
    </section>
  )
}
