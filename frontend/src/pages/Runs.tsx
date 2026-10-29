import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Card, Input, Popconfirm, Space, Table, Tooltip, Typography, message } from 'antd'
import { RedoOutlined, ReloadOutlined, StopOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { client } from '../api/client'
import { useProjectScope } from '../hooks/useProjectScope'
import PageHeader, { ProjectPicker } from '../components/PageHeader'
import StatusTag from '../components/StatusTag'
import type { ExecutionRun } from '../types'

const ACTIVE_STATUSES = ['QUEUED', 'RUNNING', 'CANCELLING']

function duration(ms: number): string {
  if (!ms) return '-'
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`
}

export default function Runs() {
  const navigate = useNavigate()
  const { projects, projectId, setProjectId } = useProjectScope()
  const [rows, setRows] = useState<ExecutionRun[]>([])
  const [loading, setLoading] = useState(false)
  const [keyword, setKeyword] = useState('')
  const [stopping, setStopping] = useState('')
  const timer = useRef<number | null>(null)

  const load = useCallback(
    async (silent = false) => {
      if (!projectId) {
        setRows([])
        return
      }
      if (!silent) setLoading(true)
      try {
        const { data } = await client.get<ExecutionRun[]>('/api/runs', { params: { project_id: projectId, q: keyword, limit: 100 } })
        setRows(data)
      } finally {
        if (!silent) setLoading(false)
      }
    },
    [projectId, keyword],
  )

  useEffect(() => {
    void load()
  }, [load])

  // 存在排队/执行中/停止中的任务时自动刷新，让页面自己跟上进度
  useEffect(() => {
    const active = rows.some((item) => ACTIVE_STATUSES.includes(item.status))
    if (!active) {
      if (timer.current) window.clearInterval(timer.current)
      timer.current = null
      return
    }
    if (timer.current) return
    timer.current = window.setInterval(() => void load(true), 2000)
    return () => {
      if (timer.current) window.clearInterval(timer.current)
      timer.current = null
    }
  }, [rows, load])

  const stop = async (run: ExecutionRun) => {
    setStopping(run.id)
    try {
      await client.post(`/api/runs/${run.id}/stop`, { reason: '页面手动停止' })
      message.info('已发送停止信号，等待执行器退出')
      await load(true)
    } finally {
      setStopping('')
    }
  }

  const rerunFailed = async (run: ExecutionRun) => {
    const { data } = await client.post(`/api/runs/${run.id}/rerun-failed`)
    message.success(`已创建重跑任务，只执行 ${data.total_cases} 个失败用例`)
    navigate(`/runs/${data.id}`)
  }

  return (
    <>
      <PageHeader
        title="执行中心"
        description="一次执行 = 一条 ExecutionRun，下面挂着用例结果与步骤结果，证据文件存在 storage 目录。"
        picker={<ProjectPicker projects={projects} value={projectId} onChange={setProjectId} />}
        extra={
          <Space>
            <Input.Search allowClear placeholder="Run ID / Commit / Pipeline" style={{ width: 240 }} onSearch={setKeyword} />
            <Button icon={<ReloadOutlined />} onClick={() => void load()}>
              刷新
            </Button>
          </Space>
        }
      />
      <Card styles={{ body: { padding: 0 } }}>
        <Table<ExecutionRun>
          rowKey="id"
          loading={loading}
          dataSource={rows}
          pagination={{ pageSize: 20, hideOnSinglePage: true }}
          locale={{ emptyText: projectId ? '还没有执行记录，去「回归计划」点执行' : '请先选择项目' }}
          columns={[
            {
              title: 'Run ID',
              dataIndex: 'id',
              width: 150,
              render: (value: string, row) => (
                <Typography.Link onClick={() => navigate(`/runs/${row.id}`)}>
                  <Typography.Text code>{value}</Typography.Text>
                </Typography.Link>
              ),
            },
            { title: '状态', dataIndex: 'status', width: 110, render: (value: string) => <StatusTag status={value} /> },
            { title: '计划', dataIndex: 'plan_name', width: 140, render: (v: string) => v || '-' },
            { title: '环境', dataIndex: 'environment_name', width: 170, render: (v: string) => v || '-' },
            {
              title: '用例统计',
              width: 260,
              render: (_, row) => (
                <Space size={4} wrap>
                  <Tooltip title="总数">
                    <Typography.Text>{row.total_cases}</Typography.Text>
                  </Tooltip>
                  <Typography.Text type="success">通过 {row.passed_cases}</Typography.Text>
                  <Typography.Text type={row.failed_cases ? 'danger' : 'secondary'}>失败 {row.failed_cases}</Typography.Text>
                  {row.flaky_cases > 0 && (
                    <Tooltip title="首次失败、重试后通过">
                      <Typography.Text type="warning">Flaky {row.flaky_cases}</Typography.Text>
                    </Tooltip>
                  )}
                  {row.cancelled_cases > 0 && <Typography.Text>停止 {row.cancelled_cases}</Typography.Text>}
                  {row.error_cases > 0 && <Typography.Text type="warning">错误 {row.error_cases}</Typography.Text>}
                  {row.skipped_cases > 0 && <Typography.Text type="secondary">跳过 {row.skipped_cases}</Typography.Text>}
                </Space>
              ),
            },
            { title: '耗时', dataIndex: 'duration_ms', width: 100, render: (value: number) => duration(value) },
            { title: '触发', dataIndex: 'trigger_source', width: 90 },
            {
              title: '来源',
              width: 140,
              render: (_, row) =>
                row.source_run_id ? (
                  <Tooltip title={`由 ${row.source_run_id} 重跑失败用例而来`}>
                    <Typography.Link onClick={() => navigate(`/runs/${row.source_run_id}`)}>
                      <Typography.Text code>{row.source_run_id}</Typography.Text>
                    </Typography.Link>
                  </Tooltip>
                ) : (
                  '-'
                ),
            },
            {
              title: '开始时间',
              dataIndex: 'started_at',
              width: 180,
              render: (value: string | null) => (value ? new Date(value).toLocaleString('zh-CN') : '-'),
            },
            {
              title: '操作',
              width: 290,
              render: (_, row) => {
                const isActive = ACTIVE_STATUSES.includes(row.status)
                return (
                  <Space size={6}>
                    <Button size="small" onClick={() => navigate(`/runs/${row.id}`)}>
                      详情
                    </Button>
                    {isActive ? (
                      <Button
                        size="small"
                        danger
                        icon={<StopOutlined />}
                        loading={stopping === row.id}
                        disabled={row.status === 'CANCELLING'}
                        onClick={() => void stop(row)}
                      >
                        {row.status === 'CANCELLING' ? '停止中' : '停止'}
                      </Button>
                    ) : (
                      <Tooltip title={row.failed_cases === 0 ? '本次执行没有失败用例' : `只重跑 ${row.failed_cases} 个失败用例`}>
                        <Button
                          size="small"
                          icon={<RedoOutlined />}
                          disabled={row.failed_cases === 0}
                          onClick={() => void rerunFailed(row)}
                        >
                          重跑失败用例
                        </Button>
                      </Tooltip>
                    )}
                  </Space>
                )
              },
            },
          ]}
        />
      </Card>
    </>
  )
}
