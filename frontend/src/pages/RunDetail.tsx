import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert, Button, Card, Collapse, Descriptions, Empty, Image, Modal, Segmented, Space, Statistic, Table, Tag, Typography, message,
} from 'antd'
import {
  ArrowLeftOutlined, DownloadOutlined, FileTextOutlined, PlayCircleOutlined, RedoOutlined, ReloadOutlined, StopOutlined,
} from '@ant-design/icons'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { client } from '../api/client'
import StatusTag from '../components/StatusTag'
import type { Artifact, Attempt, CaseResult, ExecutionRun, StepResult } from '../types'

const ACTIVE_STATUSES = ['QUEUED', 'RUNNING', 'CANCELLING']

/** M4：触发来源 → 给人看的提供方名称。 */
const CI_PROVIDER_LABEL: Record<string, string> = {
  GITLAB: 'GitLab CI',
  CLI: 'CLI',
  CI: 'CI',
}

function duration(ms: number): string {
  if (!ms) return '-'
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`
}

function sizeText(bytes: number): string {
  if (!bytes) return ''
  return bytes > 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`
}

export default function RunDetail() {
  const { runId = '' } = useParams()
  const [searchParams] = useSearchParams()
  const focusCaseId = searchParams.get('case') ?? ''
  const navigate = useNavigate()
  const [run, setRun] = useState<ExecutionRun | null>(null)
  const [cases, setCases] = useState<CaseResult[]>([])
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [attemptIndex, setAttemptIndex] = useState<Record<string, number>>({})
  const [videoOpen, setVideoOpen] = useState<{ url: string; label: string } | null>(null)
  const [loading, setLoading] = useState(false)
  const [stopping, setStopping] = useState(false)
  const timer = useRef<number | null>(null)

  const load = useCallback(
    async (silent = false) => {
      if (!runId) return
      if (!silent) setLoading(true)
      try {
        const [runData, caseData, artifactData] = await Promise.all([
          client.get<ExecutionRun>(`/api/runs/${runId}`),
          client.get<CaseResult[]>(`/api/runs/${runId}/cases`),
          client.get<Artifact[]>(`/api/runs/${runId}/artifacts`),
        ])
        setRun(runData.data)
        setCases(caseData.data)
        setArtifacts(artifactData.data)
      } finally {
        if (!silent) setLoading(false)
      }
    },
    [runId],
  )

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (!run || !ACTIVE_STATUSES.includes(run.status)) {
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
  }, [run, load])

  // 证据索引：按 attempt 与按 step 两个维度
  const byAttempt = useMemo(() => {
    const map = new Map<string, Artifact[]>()
    for (const item of artifacts) {
      if (!item.attempt_id) continue
      map.set(item.attempt_id, [...(map.get(item.attempt_id) ?? []), item])
    }
    return map
  }, [artifacts])

  const byStep = useMemo(() => {
    const map = new Map<string, Artifact[]>()
    for (const item of artifacts) {
      if (!item.step_result_id) continue
      map.set(item.step_result_id, [...(map.get(item.step_result_id) ?? []), item])
    }
    return map
  }, [artifacts])

  if (!run) {
    return (
      <Card loading={loading}>
        <Empty description="没有找到该执行记录" />
      </Card>
    )
  }

  const isActive = ACTIVE_STATUSES.includes(run.status)

  const renderAttempt = (caseItem: CaseResult, attempt: Attempt) => {
    const attemptArtifacts = byAttempt.get(attempt.id) ?? []
    const pick = (type: string) => attemptArtifacts.find((item) => item.type === type)
    const trace = pick('trace')
    const video = pick('video')
    const har = pick('har')
    const consoleLog = pick('console')
    const networkLog = pick('network')

    return (
      <>
        <Space wrap style={{ marginBottom: 12 }}>
          <StatusTag status={attempt.status} />
          <Typography.Text type="secondary">耗时 {duration(attempt.duration_ms)}</Typography.Text>
          {attempt.error_message && (
            <Typography.Text type="danger" style={{ fontSize: 12, maxWidth: 640 }} ellipsis={{ tooltip: attempt.error_message }}>
              {attempt.error_message}
            </Typography.Text>
          )}
        </Space>

        <Space wrap style={{ marginBottom: 12 }}>
          {video?.url && (
            <Button size="small" icon={<PlayCircleOutlined />} onClick={() => setVideoOpen({ url: video.url, label: `第 ${attempt.attempt_no} 次尝试录屏` })}>
              播放录屏 {sizeText(video.size)}
            </Button>
          )}
          {trace?.url && (
            <Button size="small" icon={<DownloadOutlined />} href={trace.url} target="_blank">
              下载 Trace {sizeText(trace.size)}
            </Button>
          )}
          {har?.url && (
            <Button size="small" icon={<DownloadOutlined />} href={har.url} target="_blank">
              下载 HAR {sizeText(har.size)}
            </Button>
          )}
          {consoleLog?.url && (
            <Button size="small" icon={<FileTextOutlined />} href={consoleLog.url} target="_blank">
              Console
            </Button>
          )}
          {networkLog?.url && (
            <Button size="small" icon={<FileTextOutlined />} href={networkLog.url} target="_blank">
              Network
            </Button>
          )}
        </Space>

        <Table<StepResult>
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={attempt.step_results}
          columns={[
            { title: '#', dataIndex: 'step_no', width: 50 },
            { title: '状态', dataIndex: 'status', width: 100, render: (value: string) => <StatusTag status={value} /> },
            { title: 'Action', dataIndex: 'action', width: 130, render: (value: string) => <Tag>{value}</Tag> },
            { title: '步骤名称', dataIndex: 'step_name', width: 190 },
            {
              title: '预期 / 实际',
              render: (_, row) => (
                <Space direction="vertical" size={0} style={{ fontSize: 12 }}>
                  {row.expected && <span>预期：{row.expected}</span>}
                  {row.actual && <span>实际：{row.actual}</span>}
                  {!row.expected && !row.actual && <Typography.Text type="secondary">-</Typography.Text>}
                </Space>
              ),
            },
            { title: '耗时', dataIndex: 'duration_ms', width: 90, render: (value: number) => duration(value) },
            {
              title: '错误',
              render: (_, row) =>
                row.error ? (
                  <Typography.Text type="danger" style={{ fontSize: 12 }}>
                    {row.error.slice(0, 150)}
                  </Typography.Text>
                ) : (
                  '-'
                ),
            },
            {
              title: 'Page URL',
              width: 200,
              render: (_, row) =>
                row.page_url ? (
                  <Typography.Text style={{ fontSize: 12 }} ellipsis={{ tooltip: row.page_url }}>
                    {row.page_url}
                  </Typography.Text>
                ) : (
                  '-'
                ),
            },
            {
              title: '证据',
              width: 170,
              render: (_, row) => {
                const list = byStep.get(row.id) ?? []
                const shot = list.find((item) => item.type === 'screenshot')
                const dom = list.find((item) => item.type === 'dom')
                const log = list.find((item) => item.type === 'log')
                return (
                  <Space size={6}>
                    {shot?.url && (
                      <Image
                        src={shot.url}
                        width={46}
                        height={32}
                        style={{ objectFit: 'cover', borderRadius: 4, border: '1px solid #eee' }}
                        preview={{ src: shot.url }}
                      />
                    )}
                    {log?.url && (
                      <Button size="small" type="link" href={log.url} target="_blank" style={{ padding: 0 }}>
                        log
                      </Button>
                    )}
                    {dom?.url && (
                      <Button size="small" type="link" href={dom.url} target="_blank" style={{ padding: 0 }}>
                        DOM
                      </Button>
                    )}
                    {!shot && !log && !dom && <Typography.Text type="secondary">-</Typography.Text>}
                  </Space>
                )
              },
            },
          ]}
        />
      </>
    )
  }

  return (
    <>
      <Space style={{ marginBottom: 12 }} wrap>
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/runs')}>
          返回执行中心
        </Button>
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>
          刷新
        </Button>
        {isActive ? (
          <Button
            danger
            icon={<StopOutlined />}
            loading={stopping}
            disabled={run.status === 'CANCELLING'}
            onClick={async () => {
              setStopping(true)
              try {
                await client.post(`/api/runs/${run.id}/stop`, { reason: '详情页手动停止' })
                message.info('已发送停止信号，等待执行器退出')
                await load(true)
              } finally {
                setStopping(false)
              }
            }}
          >
            {run.status === 'CANCELLING' ? '停止中…' : '停止执行'}
          </Button>
        ) : (
          <>
            <Button
              icon={<RedoOutlined />}
              onClick={async () => {
                const { data } = await client.post(`/api/runs/${run.id}/rerun`)
                message.success('已触发重跑')
                navigate(`/runs/${data.id}`)
              }}
            >
              重跑整次
            </Button>
            <Button
              type="primary"
              icon={<RedoOutlined />}
              disabled={run.failed_cases === 0}
              onClick={async () => {
                const { data } = await client.post(`/api/runs/${run.id}/rerun-failed`)
                message.success(`已创建重跑任务，只执行 ${data.total_cases} 个失败用例`)
                navigate(`/runs/${data.id}`)
              }}
            >
              重跑失败用例{run.failed_cases > 0 ? `（${run.failed_cases}）` : ''}
            </Button>
          </>
        )}
      </Space>

      {isActive && (
        <Alert
          type={run.status === 'CANCELLING' ? 'warning' : 'info'}
          showIcon
          style={{ marginBottom: 12 }}
          message={run.status === 'CANCELLING' ? '正在停止：已发出停止信号，执行器将在当前步骤结束后关闭浏览器。' : '执行进行中，页面会自动刷新。'}
        />
      )}
      {run.cancel_requested && !isActive && run.status === 'CANCELLED' && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="本次执行被用户停止" description={run.error_message} />
      )}
      {run.error_message && !isActive && run.status !== 'CANCELLED' && (
        <Alert type="error" showIcon style={{ marginBottom: 12 }} message="失败摘要" description={run.error_message} />
      )}
      {run.source_run_id && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={
            <span>
              本次是由{' '}
              <Typography.Link onClick={() => navigate(`/runs/${run.source_run_id}`)}>
                <Typography.Text code>{run.source_run_id}</Typography.Text>
              </Typography.Link>{' '}
              重跑失败用例而来（共 {run.total_cases} 个）
            </span>
          }
        />
      )}

      <Card style={{ marginBottom: 16 }}>
        <Space size={40} wrap>
          <Statistic title="状态" valueRender={() => <StatusTag status={run.status} />} />
          <Statistic title="用例总数" value={run.total_cases} />
          <Statistic title="通过" value={run.passed_cases} valueStyle={{ color: '#3f8600' }} />
          <Statistic title="失败" value={run.failed_cases} valueStyle={{ color: run.failed_cases ? '#cf1322' : undefined }} />
          <Statistic title="Flaky" value={run.flaky_cases} valueStyle={{ color: run.flaky_cases ? '#d48806' : undefined }} />
          <Statistic title="跳过" value={run.skipped_cases} />
          <Statistic title="错误" value={run.error_cases} valueStyle={{ color: run.error_cases ? '#d46b08' : undefined }} />
          {run.cancelled_cases > 0 && <Statistic title="停止" value={run.cancelled_cases} />}
          <Statistic title="耗时" value={duration(run.duration_ms)} />
        </Space>
        <Descriptions size="small" column={3} style={{ marginTop: 20 }}>
          <Descriptions.Item label="Run ID">
            <Typography.Text code>{run.id}</Typography.Text>
          </Descriptions.Item>
          <Descriptions.Item label="回归计划">{run.plan_name || '-'}</Descriptions.Item>
          <Descriptions.Item label="执行环境">{run.environment_name || '-'}</Descriptions.Item>
          <Descriptions.Item label="触发来源">
            {run.trigger_source}
            {CI_PROVIDER_LABEL[run.trigger_source] ? (
              <Tag style={{ marginInlineStart: 6 }} color="blue">
                {CI_PROVIDER_LABEL[run.trigger_source]}
              </Tag>
            ) : null}
          </Descriptions.Item>
          <Descriptions.Item label="Commit">
            {run.commit_sha ? <Typography.Text code>{run.commit_sha.slice(0, 10)}</Typography.Text> : '-'}
          </Descriptions.Item>
          <Descriptions.Item label="分支">{run.branch || '-'}</Descriptions.Item>
          {/* M4：CI 关联信息。Pipeline 可点击直达 GitLab 那次 Pipeline。 */}
          <Descriptions.Item label="Pipeline">
            {run.pipeline_id ? (
              run.pipeline_url ? (
                <a href={run.pipeline_url} target="_blank" rel="noreferrer">
                  #{run.pipeline_id}
                </a>
              ) : (
                <Typography.Text code>#{run.pipeline_id}</Typography.Text>
              )
            ) : (
              '-'
            )}
          </Descriptions.Item>
          <Descriptions.Item label="base_url 覆盖">{run.base_url_override || '-'}</Descriptions.Item>
          <Descriptions.Item label="开始时间">{run.started_at ? new Date(run.started_at).toLocaleString('zh-CN') : '-'}</Descriptions.Item>
          <Descriptions.Item label="结束时间">{run.finished_at ? new Date(run.finished_at).toLocaleString('zh-CN') : '-'}</Descriptions.Item>
          <Descriptions.Item label="证据文件">{artifacts.length} 个</Descriptions.Item>
        </Descriptions>
      </Card>

      <Card title="用例执行结果" styles={{ body: { padding: 0 } }}>
        <Collapse
          accordion
          defaultActiveKey={(() => {
            // 支持从失败中心钻取过来：/runs/{id}?case={case_result_id}
            // 直接展开那一条，省得测试人员在几十条用例里再找一次。
            const focus = focusCaseId
            if (focus && cases.some((item) => item.id === focus)) return [focus]
            return cases.filter((item) => item.status !== 'PASSED').map((item) => item.id)
          })()}
          items={cases.map((item) => {
            const current = attemptIndex[item.id] ?? item.attempts.length - 1
            const attempts = item.attempts
            const activeAttempt = attempts[current] ?? attempts[attempts.length - 1]
            return {
              key: item.id,
              label: (
                <Space size={12} wrap>
                  <StatusTag status={item.status} />
                  <Typography.Text strong>{item.case_code || item.case_id}</Typography.Text>
                  <Typography.Text>{item.case_name}</Typography.Text>
                  <Typography.Text type="secondary">{duration(item.duration_ms)}</Typography.Text>
                  <Tag color={item.attempts_count > 1 ? 'gold' : 'default'}>尝试 {item.attempts_count} 次</Tag>
                  {item.error_message && (
                    <Typography.Text type="danger" ellipsis style={{ maxWidth: 420 }}>
                      {item.error_message}
                    </Typography.Text>
                  )}
                </Space>
              ),
              children: (
                <>
                  {attempts.length > 1 && (
                    <Space style={{ marginBottom: 12 }} wrap>
                      <Typography.Text type="secondary">查看第几次尝试：</Typography.Text>
                      <Segmented
                        value={current}
                        onChange={(value) => setAttemptIndex((prev) => ({ ...prev, [item.id]: Number(value) }))}
                        options={attempts.map((attempt, index) => ({
                          label: `第 ${attempt.attempt_no} 次 · ${attempt.status}`,
                          value: index,
                        }))}
                      />
                    </Space>
                  )}
                  {activeAttempt ? renderAttempt(item, activeAttempt) : <Empty description="没有 attempt 记录" />}
                </>
              ),
            }
          })}
        />
      </Card>

      <Modal
        open={videoOpen !== null}
        title={videoOpen?.label ?? '录屏'}
        onCancel={() => setVideoOpen(null)}
        footer={null}
        width={880}
        destroyOnClose
      >
        {videoOpen?.url && (
          <video src={videoOpen.url} controls style={{ width: '100%', borderRadius: 8, background: '#000' }}>
            当前浏览器不支持内嵌播放，可
            <a href={videoOpen.url} target="_blank" rel="noreferrer">
              直接下载
            </a>
            。
          </video>
        )}
      </Modal>
    </>
  )
}
