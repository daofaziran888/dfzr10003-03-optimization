import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/ads'
const columns = [
  '流水编号',
  '输入表名',
  '输出表名',
  '输出格式',
  '推送目标',
  '推送周期',
  '可消费版本',
  '可消费窗口',
  '消费状态',
]
const actions = ['启动输出', '暂停输出', '归档流水']

type Row = Record<string, any>

export default function AdsView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => ['流水编号', '输入表名', '输出表名'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('应用层输出列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '应用层输出列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const stats = useMemo(() => [
    { label: '输出流水总数', value: total },
    { label: '可消费已生效', value: rows.filter((row) => row['消费状态'] === '已生效').length },
    { label: '沿用上一版本', value: rows.filter((row) => String(row['消费状态']).includes('沿用')).length },
    { label: '待复核', value: rows.reduce((sum, row) => sum + (Number(row['待复核数量']) || 0), 0) },
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
        throw new Error(payload.message ?? '应用层输出动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '应用层输出操作失败')
    }
  }

  async function syncConsumable(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/consumable`)
      if (!response.ok) {
        throw new Error('可消费版本读取失败')
      }
      const payload = await response.json()
      const pending = Number(payload['待复核数量']) || 0
      setNotice(
        `${row['流水编号']} 当前可消费版本：${payload['可消费版本'] ?? '无'}（窗口 ${payload['可消费窗口'] || '—'}，${payload['消费状态']}）`
        + (pending ? `；待复核分区 ${payload['待复核分区']} 暂不消费` : ''),
      )
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '可消费版本同步失败')
    }
  }

  return (
    <section className="page" data-module="ads">
      <header className="page-head">
        <div>
          <h2>应用层输出管理</h2>
          <p className="page-desc">输出页同步汇总层当前可消费版本；待复核结果不进入下游消费，旧窗口按上一版本继续提供。</p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出应用层输出清单
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
            <th>待复核</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={row['待复核数量'] ? 'row-warn' : undefined}>
              {columns.map((column) => <td key={column}>{column === '消费状态' ? formatState(row[column]) : (row[column] ?? '—')}</td>)}
              <td>
                {row['待复核数量']
                  ? `${row['待复核数量']} 个分区（${row['待复核分区']}）`
                  : '无'}
              </td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void syncConsumable(row)}>同步可消费版本</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无应用层输出数据，可先登记输出流水</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条应用层输出记录</span>
        <span className="page-desc">可消费版本由汇总层核对通过后自动切换</span>
      </footer>
    </section>
  )
}

function formatState(value: any) {
  if (!value) return '—'
  return String(value)
}
