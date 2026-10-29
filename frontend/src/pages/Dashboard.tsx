import { useCallback, useEffect, useState } from 'react'
import { Card, Col, Empty, Row, Space, Statistic, Table, Typography } from 'antd'
import { useNavigate } from 'react-router-dom'
import { client } from '../api/client'
import { useProjectScope } from '../hooks/useProjectScope'
import PageHeader, { ProjectPicker } from '../components/PageHeader'
import StatusTag from '../components/StatusTag'
import type { ExecutionRun } from '../types'

interface FailureGroupBrief {
  id: string
  title: string
  category: string
  rule_id: string
  status: string
  occurrence_count: number
  failed_case_count: number
  failed_run_count: number
  flaky_count: number
  last_seen_at: string | null
  latest_run_id: string
  fingerprint: string
  strategy: string
}

interface Summary {
  total_runs: number
  passed: number
  failed: number
  skipped: number
  flaky: number
  pass_rate: number
  running: number
  recent_runs: ExecutionRun[]
  // M3 失败中心
  failure_groups: number
  open_failure_groups: number
  failure_categories: Record<string, number>
  recent_failure_groups: FailureGroupBrief[]
  flaky_case_executions: number
  case_executions: number
  flaky_rate: number
}

const CATEGORY_META: Record<string, { color: string; label: string }> = {
  PRODUCT_BUG: { color: '#cf1322', label: '疑似产品问题' },
  ENVIRONMENT_ERROR: { color: '#d46b08', label: '环境问题' },
  AUTOMATION_ERROR: { color: '#2f54eb', label: '自动化问题' },
  DATA_ERROR: { color: '#722ed1', label: '数据问题' },
  FLAKY: { color: '#d48806', label: 'Flaky' },
  UNKNOWN: { color: '#595959', label: '证据不足' },
}

const CATEGORY_ORDER = ['PRODUCT_BUG', 'ENVIRONMENT_ERROR', 'AUTOMATION_ERROR', 'DATA_ERROR', 'FLAKY', 'UNKNOWN']

export default function Dashboard() {
  const navigate = useNavigate()
  const { projects, projectId, setProjectId } = useProjectScope()
  const [summary, setSummary] = useState<Summary | null>(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    if (!projectId) {
      setSummary(null)
      return
    }
    setLoading(true)
    try {
      const { data } = await client.get<Summary>(`/api/projects/${projectId}/dashboard/summary`)
      setSummary(data)
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    void load()
  }, [load])

  const recent = summary?.recent_runs ?? []

  return (
    <>
      <PageHeader
        title="工作台"
        description="打开就能知道：今天跑了多少、通过率如何、哪些还没跑完。"
        picker={<ProjectPicker projects={projects} value={projectId} onChange={setProjectId} />}
      />

      {!projectId ? (
        <Card>
          <Empty description="还没有项目，先去「项目」页面创建一个" />
        </Card>
      ) : (
        <>
          <Row gutter={16} style={{ marginBottom: 16 }}>
            <Col span={4}>
              <Card loading={loading}>
                <Statistic title="执行总次数" value={summary?.total_runs ?? 0} />
              </Card>
            </Col>
            <Col span={4}>
              <Card loading={loading}>
                <Statistic title="通过率" value={summary?.pass_rate ?? 0} suffix="%" precision={1} />
              </Card>
            </Col>
            <Col span={4}>
              <Card loading={loading}>
                <Statistic title="通过用例" value={summary?.passed ?? 0} valueStyle={{ color: '#3f8600' }} />
              </Card>
            </Col>
            <Col span={4}>
              <Card loading={loading}>
                <Statistic title="失败用例" value={summary?.failed ?? 0} valueStyle={{ color: summary?.failed ? '#cf1322' : undefined }} />
              </Card>
            </Col>
            <Col span={4}>
              <Card loading={loading}>
                <Statistic title="Flaky 用例" value={summary?.flaky ?? 0} valueStyle={{ color: summary?.flaky ? '#d48806' : undefined }} />
              </Card>
            </Col>
            <Col span={4}>
              <Card loading={loading}>
                <Statistic title="执行中" value={summary?.running ?? 0} valueStyle={{ color: summary?.running ? '#1677ff' : undefined }} />
              </Card>
            </Col>
          </Row>

          <Card
            title="失败中心概览"
            style={{ marginBottom: 16 }}
            extra={
              <Typography.Link onClick={() => navigate('/failures')}>
                去失败中心处理 <Typography.Text type="secondary">（{summary?.open_failure_groups ?? 0} 个待处理）</Typography.Text>
              </Typography.Link>
            }
          >
            <Row gutter={16} style={{ marginBottom: 16 }}>
              <Col span={6}>
                <Statistic title="失败分组总数" value={summary?.failure_groups ?? 0} />
              </Col>
              <Col span={6}>
                <Statistic
                  title="待处理（OPEN）"
                  value={summary?.open_failure_groups ?? 0}
                  valueStyle={{ color: summary?.open_failure_groups ? '#cf1322' : undefined }}
                />
              </Col>
              <Col span={6}>
                <Statistic
                  title="Flaky 发生率"
                  value={summary?.flaky_rate ?? 0}
                  suffix="%"
                  precision={1}
                  valueStyle={{ color: summary?.flaky_rate ? '#d48806' : undefined }}
                />
              </Col>
              <Col span={6}>
                <Statistic
                  title="Flaky 用例执行数"
                  value={summary?.flaky_case_executions ?? 0}
                  suffix={`/ ${summary?.case_executions ?? 0}`}
                />
              </Col>
            </Row>
            <Row gutter={16}>
              {CATEGORY_ORDER.map((key) => (
                <Col span={4} key={key}>
                  <Card size="small" hoverable onClick={() => navigate('/failures')}>
                    <Statistic
                      title={CATEGORY_META[key].label}
                      value={summary?.failure_categories?.[key] ?? 0}
                      valueStyle={{ color: summary?.failure_categories?.[key] ? CATEGORY_META[key].color : undefined, fontSize: 20 }}
                    />
                  </Card>
                </Col>
              ))}
            </Row>
          </Card>

          <Card title="待处理的失败分组（Top 5）" style={{ marginBottom: 16 }}>
            <Table<FailureGroupBrief>
              rowKey="id"
              size="small"
              loading={loading}
              dataSource={summary?.recent_failure_groups ?? []}
              pagination={false}
              locale={{ emptyText: '还没有失败分组，说明目前没有失败被聚合' }}
              columns={[
                {
                  title: '问题',
                  render: (_, row) => (
                    <Space direction="vertical" size={0}>
                      <Typography.Text strong>{row.title}</Typography.Text>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {row.strategy} · {row.fingerprint.slice(0, 16)}…
                      </Typography.Text>
                    </Space>
                  ),
                },
                {
                  title: '分类',
                  width: 130,
                  render: (_, row) => (
                    <Typography.Text style={{ color: CATEGORY_META[row.category]?.color }}>
                      {CATEGORY_META[row.category]?.label ?? row.category}
                    </Typography.Text>
                  ),
                },
                {
                  title: '影响',
                  width: 190,
                  render: (_, row) => (
                    <Space size={8}>
                      <Typography.Text>{row.failed_case_count} 个用例</Typography.Text>
                      <Typography.Text type="secondary">{row.occurrence_count} 次出现</Typography.Text>
                    </Space>
                  ),
                },
                {
                  title: '最近发生',
                  dataIndex: 'last_seen_at',
                  width: 172,
                  render: (v: string | null) => (v ? new Date(v).toLocaleString('zh-CN') : '-'),
                },
                {
                  title: '操作',
                  width: 120,
                  render: () => (
                    <Typography.Link onClick={() => navigate('/failures')}>去处理</Typography.Link>
                  ),
                },
              ]}
            />
          </Card>

          <Card title="最近执行" styles={{ body: { padding: 0 } }}>
            <Table<ExecutionRun>
              rowKey="id"
              size="small"
              loading={loading}
              dataSource={recent}
              pagination={false}
              locale={{ emptyText: '还没有执行记录，去「回归计划」点执行' }}
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
                {
                  title: '结果',
                  render: (_, row) => (
                    <Space size={10} wrap>
                      <Typography.Text>共 {row.total_cases}</Typography.Text>
                      <Typography.Text type="success">通过 {row.passed_cases}</Typography.Text>
                      <Typography.Text type={row.failed_cases ? 'danger' : 'secondary'}>失败 {row.failed_cases}</Typography.Text>
                      {row.flaky_cases > 0 && <Typography.Text type="warning">Flaky {row.flaky_cases}</Typography.Text>}
                      {row.cancelled_cases > 0 && <Typography.Text>停止 {row.cancelled_cases}</Typography.Text>}
                      {row.error_cases > 0 && <Typography.Text type="warning">错误 {row.error_cases}</Typography.Text>}
                    </Space>
                  ),
                },
                { title: '耗时', dataIndex: 'duration_ms', width: 110, render: (v: number) => (v ? `${(v / 1000).toFixed(1)} s` : '-') },
                {
                  title: '时间',
                  dataIndex: 'started_at',
                  width: 180,
                  render: (v: string | null) => (v ? new Date(v).toLocaleString('zh-CN') : '-'),
                },
              ]}
            />
          </Card>
        </>
      )}
    </>
  )
}
