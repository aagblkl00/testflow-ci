import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Segmented,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import { ReloadOutlined, RightOutlined } from '@ant-design/icons'
import { client } from '../api/client'
import { useProjectScope } from '../hooks/useProjectScope'
import PageHeader, { ProjectPicker } from '../components/PageHeader'
import StatusTag from '../components/StatusTag'
import FailureInsights from '../components/FailureInsights'
import type {
  FailureAffectedCase,
  FailureGroup,
  FailureGroupPage,
  FailureOccurrence,
  FailureOccurrencePage,
  FailureTrend,
} from '../types'

const { Text, Paragraph } = Typography

/** 六类的展示元信息。颜色刻意区分开：产品/环境/自动化/数据/Flaky/未知 */
const CATEGORY_META: Record<string, { color: string; label: string; hint: string }> = {
  PRODUCT_BUG: { color: 'red', label: '疑似产品问题', hint: '被测系统行为与明确预期不一致' },
  ENVIRONMENT_ERROR: { color: 'orange', label: '环境问题', hint: '网络/网关/环境配置层面异常' },
  AUTOMATION_ERROR: { color: 'geekblue', label: '自动化问题', hint: '定位器或流程配置问题' },
  DATA_ERROR: { color: 'purple', label: '数据问题', hint: '前置测试数据缺失或不可用' },
  FLAKY: { color: 'gold', label: 'Flaky', hint: '首次失败、重试通过，结果不稳定' },
  UNKNOWN: { color: 'default', label: '证据不足', hint: '现有证据无法确定性归因（M5 由 AI 辅助）' },
}

const CATEGORY_ORDER = ['PRODUCT_BUG', 'ENVIRONMENT_ERROR', 'AUTOMATION_ERROR', 'DATA_ERROR', 'FLAKY', 'UNKNOWN']

function categoryTag(category: string) {
  const meta = CATEGORY_META[category] ?? CATEGORY_META.UNKNOWN
  return (
    <Tooltip title={meta.hint}>
      <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>
        {meta.label}
      </Tag>
    </Tooltip>
  )
}

function time(value: string | null): string {
  return value ? new Date(value).toLocaleString('zh-CN') : '-'
}

const RETRY_SOURCE_LABEL: Record<string, string> = {
  blocked: '被安全门禁拦下 → 不重试',
  explicit_retry_count: '计划的显式 retry_count 生效（不受分类影响）',
  policy: '按失败分类取策略值',
  flaky_history: '该用例历史上是 Flaky，给一次重试',
  disabled: '未开启自动重试 → 不重试',
}

export default function Failures() {
  const navigate = useNavigate()
  const { projects, projectId, setProjectId } = useProjectScope()
  const [data, setData] = useState<FailureGroupPage | null>(null)
  const [loading, setLoading] = useState(false)
  const [category, setCategory] = useState('')
  const [status, setStatus] = useState('')
  const [keyword, setKeyword] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)

  const [detail, setDetail] = useState<FailureGroup | null>(null)
  const [occurrences, setOccurrences] = useState<FailureOccurrencePage | null>(null)
  const [occPage, setOccPage] = useState(1)
  const [trend, setTrend] = useState<FailureTrend | null>(null)
  const [cases, setCases] = useState<FailureAffectedCase[]>([])
  const [detailLoading, setDetailLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const { data: payload } = await client.get<FailureGroupPage>('/api/failures', {
        params: {
          project_id: projectId || undefined,
          category: category || undefined,
          status: status || undefined,
          q: keyword || undefined,
          page,
          page_size: pageSize,
        },
      })
      setData(payload)
    } catch {
      message.error('加载失败分组失败')
    } finally {
      setLoading(false)
    }
  }, [projectId, category, status, keyword, page, pageSize])

  useEffect(() => {
    void load()
  }, [load])

  const loadDetail = useCallback(
    async (group: FailureGroup, targetPage = 1) => {
      setDetailLoading(true)
      try {
        const [detailRes, occRes, trendRes, caseRes] = await Promise.all([
          client.get<FailureGroup>(`/api/failures/${group.id}`),
          client.get<FailureOccurrencePage>(`/api/failures/${group.id}/occurrences`, {
            params: { page: targetPage, page_size: 10 },
          }),
          client.get<FailureTrend>(`/api/failures/${group.id}/trend`, { params: { limit: 7 } }),
          client.get<FailureAffectedCase[]>(`/api/failures/${group.id}/cases`, { params: { limit: 50 } }),
        ])
        setDetail(detailRes.data)
        setOccurrences(occRes.data)
        setTrend(trendRes.data)
        setCases(caseRes.data)
        setOccPage(targetPage)
      } catch {
        message.error('加载失败分组详情失败')
      } finally {
        setDetailLoading(false)
      }
    },
    [],
  )

  const changeStatus = async (group: FailureGroup, next: string) => {
    try {
      await client.patch(`/api/failures/${group.id}`, { status: next })
      message.success(`已标记为 ${next}`)
      if (detail?.id === group.id) setDetail({ ...detail, status: next })
      void load()
    } catch {
      message.error('状态更新失败')
    }
  }

  const counts = data?.category_counts ?? {}
  const total = Object.values(counts).reduce((sum, item) => sum + item, 0)

  const tabItems = [
    { key: '', label: `全部 (${total})` },
    ...CATEGORY_ORDER.map((key) => ({
      key,
      label: `${CATEGORY_META[key].label} (${counts[key] ?? 0})`,
    })),
  ]

  return (
    <>
      <PageHeader
        title="失败中心"
        description="一次回归失败几十个用例，实际可能只有几个问题。这里按确定性指纹把失败聚合成可调查的问题。"
        picker={<ProjectPicker projects={projects} value={projectId} onChange={(value) => { setProjectId(value); setPage(1) }} />}
        extra={
          <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
        }
      />

      <Card size="small" bodyStyle={{ paddingBottom: 0 }}>
        <Tabs
          activeKey={category}
          items={tabItems}
          onChange={(key) => {
            setCategory(key)
            setPage(1)
          }}
        />
        <Space wrap style={{ marginBottom: 12 }}>
          <Segmented
            value={status}
            onChange={(value) => {
              setStatus(String(value))
              setPage(1)
            }}
            options={[
              { label: '全部状态', value: '' },
              { label: 'OPEN', value: 'OPEN' },
              { label: 'RESOLVED', value: 'RESOLVED' },
              { label: 'IGNORED', value: 'IGNORED' },
            ]}
          />
          <Input.Search
            allowClear
            placeholder="搜索标题 / 规则 / 分组 ID"
            style={{ width: 260 }}
            onSearch={(value) => {
              setKeyword(value.trim())
              setPage(1)
            }}
          />
        </Space>
      </Card>

      <Card size="small" style={{ marginTop: 12 }}>
        <Table<FailureGroup>
          rowKey="id"
          size="small"
          loading={loading}
          dataSource={data?.items ?? []}
          locale={{ emptyText: <Empty description="当前筛选下没有失败分组" /> }}
          pagination={{
            current: data?.page ?? 1,
            pageSize: data?.page_size ?? 20,
            total: data?.total ?? 0,
            showSizeChanger: true,
            pageSizeOptions: ['10', '20', '50'],
            showTotal: (value) => `共 ${value} 个失败分组`,
            onChange: (nextPage, nextSize) => {
              setPage(nextPage)
              setPageSize(nextSize)
            },
          }}
          onRow={(row) => ({
            onClick: () => void loadDetail(row),
            style: { cursor: 'pointer' },
          })}
          columns={[
            {
              title: '失败分组',
              render: (_, row) => (
                <Space direction="vertical" size={0}>
                  <Space size={6}>
                    <Text strong>{row.title || '（无标题）'}</Text>
                  </Space>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {row.id}
                    {row.fingerprint ? ` · ${row.fingerprint.strategy} · ${row.fingerprint.fingerprint.slice(0, 16)}…` : ''}
                  </Text>
                </Space>
              ),
            },
            { title: '分类', width: 118, render: (_, row) => categoryTag(row.category) },
            {
              title: '规则',
              width: 200,
              render: (_, row) => (
                <Tooltip title={row.rule_reason}>
                  <Text code style={{ fontSize: 12 }}>
                    {row.rule_id || '-'}
                  </Text>
                </Tooltip>
              ),
            },
            { title: '状态', width: 96, render: (_, row) => <StatusTag status={row.status} /> },
            {
              title: '出现',
              width: 84,
              render: (_, row) => (
                <Space size={4}>
                  <Text>{row.occurrence_count}</Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>次</Text>
                </Space>
              ),
            },
            {
              title: '影响',
              width: 168,
              render: (_, row) => (
                <Space size={8}>
                  <Text>{row.failed_case_count} 个用例</Text>
                  <Text type="secondary">{row.failed_run_count} 次执行</Text>
                </Space>
              ),
            },
            {
              title: 'Flaky',
              width: 72,
              render: (_, row) =>
                row.flaky_count > 0 ? <Text style={{ color: '#d48806' }}>{row.flaky_count}</Text> : <Text type="secondary">-</Text>,
            },
            { title: '最近发生', dataIndex: 'last_seen_at', width: 172, render: (value: string | null) => time(value) },
            {
              title: '最近执行',
              width: 110,
              render: (_, row) =>
                row.latest_run_id ? (
                  <a onClick={() => navigate(`/runs/${row.latest_run_id}`)}>{row.latest_run_id}</a>
                ) : (
                  '-'
                ),
            },
            {
              title: '操作',
              width: 96,
              fixed: 'right',
              render: (_, row) => (
                <Button size="small" type="link" onClick={() => void loadDetail(row)}>
                  详情 <RightOutlined />
                </Button>
              ),
            },
          ]}
        />
      </Card>

      <Drawer
        open={Boolean(detail)}
        width={860}
        title={detail ? `失败分组 · ${detail.title || detail.id}` : ''}
        onClose={() => setDetail(null)}
        loading={detailLoading}
        extra={
          detail && (
            <Space>
              <Button size="small" onClick={() => void changeStatus(detail, 'IGNORED')} disabled={detail.status === 'IGNORED'}>
                忽略
              </Button>
              <Button size="small" onClick={() => void changeStatus(detail, 'RESOLVED')} disabled={detail.status === 'RESOLVED'}>
                标记已解决
              </Button>
              <Button size="small" type="primary" onClick={() => void changeStatus(detail, 'OPEN')} disabled={detail.status === 'OPEN'}>
                重新打开
              </Button>
            </Space>
          )
        }
      >
        {detail && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Alert
              type="info"
              showIcon
              message={`${categoryTag(detail.category)}  ${detail.rule_reason || ''}`}
              description={
                <Space direction="vertical" size={2}>
                  <Text>
                    规则 <Text code>{detail.rule_id || '-'}</Text> · 确定性分类（M3 不使用 AI 判断）
                  </Text>
                  <Text type="secondary">
                    重试策略：{RETRY_SOURCE_LABEL[String(detail.retry_policy?.source ?? '')] ?? '-'}
                    {detail.retry_policy?.retries !== undefined ? `（${detail.retry_policy.retries} 次）` : ''}
                  </Text>
                </Space>
              }
            />

            <Descriptions title="基本信息" size="small" column={2} bordered>
              <Descriptions.Item label="分组 ID">{detail.id}</Descriptions.Item>
              <Descriptions.Item label="状态">
                <StatusTag status={detail.status} />
              </Descriptions.Item>
              <Descriptions.Item label="首次出现">{time(detail.first_seen_at)}</Descriptions.Item>
              <Descriptions.Item label="最近出现">{time(detail.last_seen_at)}</Descriptions.Item>
              <Descriptions.Item label="出现次数">{detail.occurrence_count}</Descriptions.Item>
              <Descriptions.Item label="影响用例 / 执行">
                {detail.failed_case_count} 个 / {detail.failed_run_count} 次
              </Descriptions.Item>
              <Descriptions.Item label="其中 Flaky">{detail.flaky_count}</Descriptions.Item>
              <Descriptions.Item label="所属项目">{detail.project_name || detail.project_id}</Descriptions.Item>
            </Descriptions>

            <Descriptions title="Failure Fingerprint" size="small" column={1} bordered>
              <Descriptions.Item label="指纹">
                <Text code copyable>
                  {detail.fingerprint?.fingerprint ?? '-'}
                </Text>
              </Descriptions.Item>
              <Descriptions.Item label="策略 / 类型">
                {detail.fingerprint?.strategy ?? '-'} / {detail.fingerprint?.failure_type ?? '-'}
              </Descriptions.Item>
              <Descriptions.Item label="归一化错误">
                <Paragraph style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}>
                  {detail.fingerprint?.normalized_message || '-'}
                </Paragraph>
              </Descriptions.Item>
              <Descriptions.Item label="期望 / 实际">
                <Text type="secondary">期望：</Text>
                {detail.fingerprint?.normalized_expected || '-'}
                <br />
                <Text type="secondary">实际：</Text>
                {detail.fingerprint?.normalized_actual || '-'}
              </Descriptions.Item>
              <Descriptions.Item label="请求">
                {detail.fingerprint?.http_status
                  ? `${detail.fingerprint.request_method} ${detail.fingerprint.request_url} → ${detail.fingerprint.http_status}`
                  : detail.fingerprint?.request_url || '-'}
              </Descriptions.Item>
              <Descriptions.Item label="页面 / 定位器">
                {detail.fingerprint?.page_url || '-'}
                {detail.fingerprint?.locator ? ` · ${detail.fingerprint.locator}` : ''}
              </Descriptions.Item>
            </Descriptions>

            <Card size="small" title={`出现趋势（最近 ${trend?.points.length ?? 0} 次相关执行）`}>
              {trend && trend.points.length > 0 ? (
                <Table
                  rowKey="run_id"
                  size="small"
                  pagination={false}
                  dataSource={trend.points}
                  columns={[
                    { title: '执行时间', dataIndex: 'run_started_at', render: (value: string | null) => time(value) },
                    {
                      title: 'Run',
                      dataIndex: 'run_id',
                      render: (value: string) => <a onClick={() => navigate(`/runs/${value}`)}>{value}</a>,
                    },
                    { title: '状态', dataIndex: 'run_status', render: (value: string) => <StatusTag status={value} /> },
                    { title: '出现', dataIndex: 'occurrences' },
                    { title: '影响用例', dataIndex: 'cases' },
                    {
                      title: 'Flaky',
                      dataIndex: 'flaky_occurrences',
                      render: (value: number) => (value > 0 ? <Text style={{ color: '#d48806' }}>{value}</Text> : '-'),
                    },
                  ]}
                />
              ) : (
                <Empty description="暂无出现记录" />
              )}
            </Card>

            <Card size="small" title={`影响用例（${cases.length}）`}>
              <Table<FailureAffectedCase>
                rowKey={(row) => `${row.case_id}-${row.case_code}`}
                size="small"
                pagination={false}
                dataSource={cases}
                locale={{ emptyText: <Empty description="暂无" /> }}
                columns={[
                  { title: '用例编号', dataIndex: 'case_code', width: 180 },
                  { title: '用例名称', dataIndex: 'case_name' },
                  { title: '出现次数', dataIndex: 'occurrences', width: 96 },
                  {
                    title: 'Flaky',
                    dataIndex: 'is_flaky',
                    width: 80,
                    render: (value: boolean) => (value ? <Tag color="gold">是</Tag> : '-'),
                  },
                  { title: '最近发生', dataIndex: 'last_seen_at', width: 172, render: (value: string | null) => time(value) },
                ]}
              />
            </Card>

            <Card
              size="small"
              title="出现历史（可钻取到真实执行证据）"
              extra={
                <Text type="secondary" style={{ fontSize: 12 }}>
                  分组 → 出现 → 执行 → 用例 → 尝试 → 步骤 → 证据
                </Text>
              }
            >
              <Table<FailureOccurrence>
                rowKey="id"
                size="small"
                loading={detailLoading}
                dataSource={occurrences?.items ?? []}
                locale={{ emptyText: <Empty description="暂无" /> }}
                pagination={{
                  current: occPage,
                  pageSize: 10,
                  total: occurrences?.total ?? 0,
                  showTotal: (value) => `共 ${value} 条`,
                  onChange: (nextPage) => void loadDetail(detail, nextPage),
                }}
                columns={[
                  { title: '时间', dataIndex: 'created_at', width: 168, render: (value: string | null) => time(value) },
                  {
                    title: '执行',
                    width: 190,
                    render: (_, row) => (
                      <Space direction="vertical" size={0}>
                        <a onClick={() => navigate(`/runs/${row.run_id}?case=${row.case_result_id}`)}>{row.run_id}</a>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {row.plan_name || '-'} · {row.environment_name || '-'}
                        </Text>
                        {/* M4：CI 上下文。有 Pipeline 就给直达链接，没有就只显示分支/提交。 */}
                        {(row.run_branch || row.run_commit_sha || row.run_pipeline_id) && (
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {row.run_branch || '-'}
                            {row.run_commit_sha ? ` · ${row.run_commit_sha.slice(0, 7)}` : ''}
                            {row.run_pipeline_id
                              ? row.run_pipeline_url
                                ? (
                                    <>
                                      {' · '}
                                      <a href={row.run_pipeline_url} target="_blank" rel="noreferrer">
                                        Pipeline #{row.run_pipeline_id}
                                      </a>
                                    </>
                                  )
                                : ` · Pipeline #${row.run_pipeline_id}`
                              : ''}
                          </Text>
                        )}
                      </Space>
                    ),
                  },
                  {
                    title: '用例',
                    render: (_, row) => (
                      <Space direction="vertical" size={0}>
                        <Text>{row.case_code || row.case_result_id}</Text>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {row.case_name}
                        </Text>
                      </Space>
                    ),
                  },
                  {
                    title: '尝试 / 步骤',
                    width: 140,
                    render: (_, row) => (
                      <Space direction="vertical" size={0}>
                        <Text>
                          第 {row.attempt_no || '-'} 次 · 步骤 {row.step_no || '-'}
                        </Text>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {row.step_action || '-'}
                        </Text>
                      </Space>
                    ),
                  },
                  {
                    title: '现场',
                    width: 210,
                    render: (_, row) => (
                      <Tooltip title={row.step_error}>
                        <Text style={{ fontSize: 12 }}>{row.step_error ? `${row.step_error.slice(0, 80)}…` : '-'}</Text>
                      </Tooltip>
                    ),
                  },
                  {
                    title: '状态',
                    width: 148,
                    render: (_, row) => (
                      <Space direction="vertical" size={2}>
                        <StatusTag status={row.case_status} />
                        <Space size={4}>
                          {categoryTag(row.category)}
                          {row.is_flaky && <Tag color="gold">Flaky</Tag>}
                        </Space>
                      </Space>
                    ),
                  },
                ]}
              />
            </Card>

            {/* M5：AI 分析 / 告警 / 通知。三块内容独立取数，任何一块失败不影响其它块 */}
            <FailureInsights groupId={detail.id} />
          </Space>
        )}
      </Drawer>
    </>
  )
}
