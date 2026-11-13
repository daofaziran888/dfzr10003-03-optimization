import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dws'
const columns = [
  '流水编号',
  '输入表名',
  '输出表名',
  '汇总粒度',
  '时间窗口',
  '事实口径版本',
  '受影响窗口',
  '待处理分区',
  '重算状态',
  '可消费版本',
]
const actions = ['启动聚合', '暂停聚合', '归档流水']
const statuses = ['草稿', '运行中', '已暂停', '已完成']

type Row = Record<string, any>

type Detail = {
  plan?: Record<string, any>
  batches?: Record<string, any>[]
  partitions?: Record<string, any>[]
  reviews?: Record<string, any>[]
  history?: Record<string, any>[]
}

export default function DwsView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selected, setSelected] = useState<Row | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const filterFields = useMemo(() => ['流水编号', '输入表名', '输出表名'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('聚合流水列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '汇总层聚合列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const stats = useMemo(() => [
    { label: '聚合流水总数', value: total },
    { label: '运行中', value: rows.filter((row) => row.status === '运行中').length },
    { label: '待重算', value: rows.filter((row) => row['重算状态'] === '待重算').length },
    { label: '待复核', value: rows.reduce((sum, row) => sum + (Number(row['待复核数量']) || 0), 0) },
  ], [rows, total])

  async function post(path: string, body: Record<string, any>, okMessage: string) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message ?? '汇总层聚合动作未生效，请稍后重试')
      }
      setNotice(payload.message ?? okMessage)
      await reload()
      if (selected) {
        await openDetail({ ...selected, id: selected.id })
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '汇总层聚合操作失败')
    }
  }

  async function runAction(action: string, row: Row) {
    await post(`${ENDPOINT}/${row.id}/actions`, { action }, '动作已执行')
  }

  async function openDetail(row: Row) {
    setSelected(row)
    setDetail(null)
    try {
      const [planResponse, batchResponse] = await Promise.all([
        request(`${ENDPOINT}/${row.id}/recalc-plan`),
        request(`${ENDPOINT}/${row.id}/batches`),
      ])
      const plan = planResponse.ok ? await planResponse.json() : {}
      const batches = batchResponse.ok ? await batchResponse.json() : {}
      setDetail({
        plan,
        batches: batches['批次'] ?? [],
        partitions: batches['分区版本'] ?? [],
        reviews: batches['待复核'] ?? [],
        history: batches['history'] ?? [],
      })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '重算计划读取失败')
    }
  }

  function submitRecalculate(row: Row) {
    void post(`${ENDPOINT}/${row.id}/recalculate`, {}, '重算已提交')
  }

  function verifyLatestBatch(row: Row) {
    const batchId = row['批次ID']
    if (!batchId) {
      setErrorMessage('该流水还没有可核对的重算批次')
      return
    }
    void post(`${ENDPOINT}/${row.id}/batches/${batchId}/verify`, {}, '一致性核对已完成')
  }

  function latePartition(row: Row) {
    const form = window.prompt('迟到分区，格式如 2026-09-02（多个用逗号分隔）', row['时间窗口'] ?? '')
    if (!form) return
    const updatedAt = window.prompt('数据更新时间（用于判断是否晚于聚合批次）', '2026-09-04 08:00:00')
    if (!updatedAt) return
    void post(`${ENDPOINT}/${row.id}/late-partitions`, { 分区: form, 数据更新时间: updatedAt }, '迟到分区已处理')
  }

  function resolveReviews(row: Row) {
    void post(`${ENDPOINT}/${row.id}/reviews/resolve`, { 备注: '确认迟到数据不覆盖当前可消费版本' }, '待复核已释放')
  }

  return (
    <section className="page" data-module="dws">
      <header className="page-head">
        <div>
          <h2>汇总层聚合管理</h2>
          <p className="page-desc">按事实定义的粒度、度量与分区字段判断受影响窗口，再用汇总粒度与时间窗口界定重算范围；只重算仍待处理的分区。</p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出汇总层聚合清单
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
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={row['重算状态'] === '待复核' ? 'row-warn' : undefined}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void openDetail(row)}>重算明细</button>
                <button className="link" type="button" onClick={() => submitRecalculate(row)}>提交重算</button>
                <button className="link" type="button" onClick={() => verifyLatestBatch(row)}>一致性核对</button>
                <button className="link" type="button" onClick={() => latePartition(row)}>迟到补算</button>
                {row['待复核数量'] > 0 && (
                  <button className="link" type="button" onClick={() => resolveReviews(row)}>复核待复核数据</button>
                )}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无汇总层聚合数据，可先登记聚合流水</td>
            </tr>
          )}
        </tbody>
      </table>

      {selected && (
        <article className="detail-panel">
          <header className="detail-head">
            <h3>重算明细 · {selected['流水编号']} / {selected['输出表名']}</h3>
            <button className="btn ghost" type="button" onClick={() => setSelected(null)}>关闭</button>
          </header>
          {!detail && <p className="page-desc">正在读取重算计划与批次账本…</p>}
          {detail?.plan && (
            <div className="detail-grid">
              <div><span className="stat-label">事实口径版本</span>{detail.plan['事实口径版本'] ?? '—'}</div>
              <div><span className="stat-label">口径生效时间</span>{detail.plan['口径生效时间'] ?? '—'}</div>
              <div><span className="stat-label">事实粒度 / 事实分区</span>{detail.plan['事实粒度']} / {detail.plan['事实分区']?.join('、') || '—'}</div>
              <div><span className="stat-label">汇总粒度 / 时间窗口</span>{detail.plan['汇总粒度']} / {detail.plan['时间窗口']}</div>
              <div><span className="stat-label">受影响窗口</span>{detail.plan['受影响窗口']?.join('、') || '—'}</div>
              <div><span className="stat-label">待处理分区</span>{detail.plan['待处理分区']?.join('、') || '—'}</div>
              <div><span className="stat-label">判断依据</span>{detail.plan['判断依据'] ?? '—'}</div>
              <div><span className="stat-label">可消费版本</span>{selected['可消费版本'] ?? '尚无可消费版本'}（{selected['可消费状态']}）</div>
            </div>
          )}
          {detail?.batches && detail.batches.length > 0 && (
            <>
              <h4 className="detail-title">重算批次</h4>
              <table className="data-table compact">
                <thead><tr><th>批次编号</th><th>触发原因</th><th>口径版本</th><th>窗口</th><th>状态</th><th>核对结果</th><th>创建时间</th></tr></thead>
                <tbody>
                  {detail.batches.map((batch) => (
                    <tr key={String(batch.id)}>
                      <td>{batch['批次编号']}</td>
                      <td>{batch['触发原因']}</td>
                      <td>{batch['口径版本']}</td>
                      <td>{batch['时间窗口']}</td>
                      <td>{batch['状态']}</td>
                      <td>{batch['核对结果'] ?? '—'}</td>
                      <td>{batch['创建时间']}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          {detail?.partitions && detail.partitions.length > 0 && (
            <>
              <h4 className="detail-title">分区版本账本</h4>
              <table className="data-table compact">
                <thead><tr><th>分区</th><th>口径版本</th><th>状态</th><th>行数</th><th>批次编号</th><th>更新时间</th></tr></thead>
                <tbody>
                  {detail.partitions.map((item) => (
                    <tr key={String(item.id)} className={item['状态'] === '待复核' ? 'row-warn' : undefined}>
                      <td>{item['分区']}</td>
                      <td>{item['口径版本']}</td>
                      <td>{item['状态']}</td>
                      <td>{item['行数']}</td>
                      <td>{item['批次编号']}</td>
                      <td>{item['更新时间']}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          {detail?.reviews && detail.reviews.length > 0 && (
            <>
              <h4 className="detail-title">待复核记录</h4>
              <table className="data-table compact">
                <thead><tr><th>分区</th><th>口径版本</th><th>数据更新时间</th><th>批次时间</th><th>原因</th></tr></thead>
                <tbody>
                  {detail.reviews.map((item) => (
                    <tr key={String(item.id)}>
                      <td>{item['分区']}</td>
                      <td>{item['口径版本']}</td>
                      <td>{item['数据更新时间']}</td>
                      <td>{item['批次时间']}</td>
                      <td>{item['原因']}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </article>
      )}

      <footer className="page-foot">
        <span>共 {total} 条汇总层聚合记录</span>
        <span className="page-desc">{statuses.join(' / ')}</span>
      </footer>
    </section>
  )
}
