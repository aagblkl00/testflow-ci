import { useCallback, useEffect, useState } from 'react'
import { Alert as AntAlert, Button, Card, Descriptions, Empty, Input, Progress, Space, Table, Tag, Tooltip, Typography, message } from 'antd'
import { client } from '../api/client'
import type { AiMeta, Alert, AnalysisListResult, FailureAnalysis, NotificationLog } from '../types'

const { Text, Paragraph } = Typography

const SEVERITY_COLOR: Record<string, string> = {
  INFO: 'default',
  WARNING: 'gold',
  ERROR: 'volcano',
  CRITICAL: 'red',
}

const ALERT_STATUS_LABEL: Record<string, string> = {
  OPEN: '待处理',
  ACKNOWLEDGED: '已确认',
  FIXING: '处理中',
  REVERIFY: '待重新验证',
  RESOLVED: '已解决',
  IGNORED: '已忽略',
}

const ANALYSIS_STATUS_COLOR: Record<string, string> = {
  PENDING: 'default',
  RUNNING: 'processing',
  COMPLETED: 'green',
  FAILED: 'red',
  CANCELLED: 'default',
}

const REVIEW_LABEL: Record<string, string> = {
  UNREVIEWED: '未确认',
  CONFIRMED: '已确认',
  MODIFIED: '已修改',
  IGNORED: '已忽略',
}

/** 告警状态 → 可执行的动作（与后端状态机一一对应，不自己发明转换）。 */
const ALERT_ACTIONS: { target: string; label: string; api: string }[] = [
  { target: 'ACKNOWLEDGED', label: '确认', api: 'ack' },
  { target: 'FIXING', label: '开始处理', api: 'fixing' },
  { target: 'REVERIFY', label: '进入重新验证', api: 'reverify' },
  { target: 'RESOLVED', label: '标记已解决', api: 'resolve' },
  { target: 'IGNORED', label: '忽略', api: 'ignore' },
]

function time(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString('zh-CN') : '-'
}

/**
 * 失败分组的 M5 洞察：AI 分析 / 告警 / 通知。
 *
 * 独立成组件而不是塞进 Failures.tsx：抽屉里已经有七块内容，再加三块会让
 * 那个文件难以阅读。这里的每一块都只读它自己的接口，互不牵连。
 */
export default function FailureInsights({ groupId }: { groupId: string }) {
  const [meta, setMeta] = useState<AiMeta | null>(null)
  const [analyses, setAnalyses] = useState<AnalysisListResult | null>(null)
  const [alert, setAlert] = useState<Alert | null>(null)
  const [notifications, setNotifications] = useState<NotificationLog[]>([])
  const [busy, setBusy] = useState('')
  const [comment, setComment] = useState('')
  const [reviewComment, setReviewComment] = useState('')

  const load = useCallback(async () => {
    const [metaRes, analysisRes, alertRes] = await Promise.all([
      client.get<AiMeta>('/api/ai/meta'),
      client.get<AnalysisListResult>(`/api/failures/${groupId}/analyses`),
      client.get<{ items: Alert[] }>('/api/alerts', { params: { page_size: 200 } }),
    ])
    setMeta(metaRes.data)
    setAnalyses(analysisRes.data)
    const mine = (alertRes.data.items || []).filter((item) => item.failure_group_id === groupId)
    // 优先展示活动告警；没有就展示历史上最新的一条
    const active = mine.find((item) => ['OPEN', 'ACKNOWLEDGED', 'FIXING', 'REVERIFY'].includes(item.status))
    const chosen = active ?? mine[0] ?? null
    setAlert(chosen)
    if (chosen) {
      const logRes = await client.get<{ items: NotificationLog[] }>(`/api/alerts/${chosen.id}/notifications`)
      setNotifications(logRes.data.items || [])
    } else {
      setNotifications([])
    }
  }, [groupId])

  useEffect(() => {
    void load().catch(() => message.error('加载 AI 分析 / 告警信息失败'))
  }, [load])

  const latest: FailureAnalysis | undefined = analyses?.items?.[0]

  const runAnalyze = async (force: boolean) => {
    setBusy('analyze')
    try {
      const { data } = await client.post(`/api/failures/${groupId}/analyze`, { force })
      if (data.skipped) {
        message.info(`未执行分析：${data.reason}`)
      } else if (data.analysis?.status === 'FAILED') {
        message.warning(`分析失败：${data.analysis.error_message}`)
      } else {
        message.success(data.created ? '分析完成' : '命中已有分析（证据未变）')
      }
      await load()
    } catch {
      message.error('触发分析失败')
    } finally {
      setBusy('')
    }
  }

  const review = async (status: string) => {
    if (!latest) return
    setBusy('review')
    try {
      await client.patch(`/api/analyses/${latest.id}/review`, {
        review_status: status,
        review_comment: reviewComment,
      })
      message.success(`已标记为 ${REVIEW_LABEL[status] ?? status}`)
      setReviewComment('')
      await load()
    } catch {
      message.error('人工确认失败')
    } finally {
      setBusy('')
    }
  }

  const createAlert = async () => {
    setBusy('alert')
    try {
      await client.post(`/api/failures/${groupId}/alerts`)
      message.success('告警已建立')
      await load()
    } catch {
      message.error('建立告警失败')
    } finally {
      setBusy('')
    }
  }

  const act = async (api: string, label: string) => {
    if (!alert) return
    setBusy(api)
    try {
      const { data } = await client.post(`/api/alerts/${alert.id}/${api}`, { comment })
      if (api === 'reverify') {
        message.success(`已创建重新验证执行 ${data.run_id}（只跑 ${data.case_count} 个失败用例）`)
      } else {
        message.success(`已${label}`)
      }
      setComment('')
      await load()
    } catch (error) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      message.error(detail || `操作失败：${label}`)
    } finally {
      setBusy('')
    }
  }

  const notifyNow = async () => {
    if (!alert) return
    setBusy('notify')
    try {
      const { data } = await client.post(`/api/alerts/${alert.id}/notify`)
      if (data.status === 'SENT') message.success('通知已发送')
      else if (data.status === 'SKIPPED') message.info(`未发送：${data.reason ?? '渠道未配置'}`)
      else message.warning(`发送失败（尝试 ${data.attempt_count} 次）：${data.error}`)
      await load()
    } catch {
      message.error('发送通知失败')
    } finally {
      setBusy('')
    }
  }

  const aiReady = Boolean(meta?.ai_enabled)

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {/* ---------------------------------------------------------- AI 分析 */}
      <Card
        size="small"
        title="AI 分析"
        extra={
          <Space>
            <Button size="small" type="primary" loading={busy === 'analyze'} onClick={() => void runAnalyze(false)}>
              开始分析
            </Button>
            <Tooltip title="忽略已有分析，产生新的 revision（旧结论保留）">
              <Button size="small" loading={busy === 'analyze'} onClick={() => void runAnalyze(true)} disabled={!latest}>
                重新分析
              </Button>
            </Tooltip>
          </Space>
        }
      >
        {!aiReady && (
          <AntAlert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="AI 未配置，分析入口不会产生结果"
            description={meta?.note || '请在服务端配置 AI_API_KEY 与 AI_MODEL'}
          />
        )}
        {analyses && !analyses.eligible && (
          <AntAlert
            type="success"
            showIcon
            style={{ marginBottom: 12 }}
            message="该失败已由确定性规则判定，默认不需要 AI"
            description={`${analyses.eligibility_reason}。仍可点「开始分析」手动触发。`}
          />
        )}
        {!latest ? (
          <Empty description="还没有分析记录" />
        ) : (
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            <Space wrap size={8}>
              <Tag color={ANALYSIS_STATUS_COLOR[latest.status] ?? 'default'}>{latest.status}</Tag>
              <Tag>第 {latest.revision_no} 版</Tag>
              <Tag>{REVIEW_LABEL[latest.review_status] ?? latest.review_status}</Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {latest.provider} / {latest.model} · prompt {latest.prompt_version}
              </Text>
            </Space>
            {latest.status === 'FAILED' && (
              <AntAlert type="error" showIcon message="本次分析失败（不影响测试结果）" description={latest.error_message} />
            )}
            {latest.status === 'COMPLETED' && (
              <>
                <Descriptions size="small" column={1} bordered>
                  <Descriptions.Item label="一句话总结">{latest.summary || '-'}</Descriptions.Item>
                  <Descriptions.Item label="可能原因">
                    <Paragraph style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}>{latest.root_cause || '-'}</Paragraph>
                  </Descriptions.Item>
                  <Descriptions.Item label="证据">
                    {latest.evidence?.length ? (
                      <ul style={{ margin: 0, paddingInlineStart: 18 }}>
                        {latest.evidence.map((item, index) => (
                          <li key={index}>{item}</li>
                        ))}
                      </ul>
                    ) : (
                      '-'
                    )}
                  </Descriptions.Item>
                  <Descriptions.Item label="置信度">
                    {latest.confidence !== null ? (
                      <Space>
                        <Progress
                          percent={Math.round((latest.confidence ?? 0) * 100)}
                          size="small"
                          style={{ width: 160 }}
                          showInfo={false}
                        />
                        <Text>{Math.round((latest.confidence ?? 0) * 100)}%</Text>
                      </Space>
                    ) : (
                      '-'
                    )}
                  </Descriptions.Item>
                  <Descriptions.Item label="不确定的部分">{latest.uncertainty || '-'}</Descriptions.Item>
                  <Descriptions.Item label="建议下一步">{latest.recommendation || '-'}</Descriptions.Item>
                  <Descriptions.Item label="相关提交">{latest.related_commit || '（未能确定）'}</Descriptions.Item>
                  <Descriptions.Item label="建议关注">
                    {/* 字段名就叫「建议」——AI 不认定责任人（方案原则 4） */}
                    {latest.suggested_owner || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="AI 建议分类">
                    {latest.ai_suggested_category ? (
                      <Space>
                        <Tag color={latest.ai_suggested_category === 'UNKNOWN' ? 'default' : 'blue'}>
                          {latest.ai_suggested_category}
                        </Tag>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          仅作建议，不会覆盖系统分类
                        </Text>
                      </Space>
                    ) : (
                      <Text type="secondary">（AI 未给出建议）</Text>
                    )}
                  </Descriptions.Item>
                </Descriptions>
                <Space wrap>
                  <Input
                    size="small"
                    placeholder="确认备注（可选）"
                    style={{ width: 260 }}
                    value={reviewComment}
                    onChange={(event) => setReviewComment(event.target.value)}
                  />
                  <Button size="small" loading={busy === 'review'} onClick={() => void review('CONFIRMED')}>
                    确认
                  </Button>
                  <Button size="small" loading={busy === 'review'} onClick={() => void review('MODIFIED')}>
                    修改
                  </Button>
                  <Button size="small" danger loading={busy === 'review'} onClick={() => void review('IGNORED')}>
                    忽略
                  </Button>
                </Space>
                {latest.reviewed_at && (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    最近确认：{latest.reviewed_by || '-'} · {time(latest.reviewed_at)}
                    {latest.review_comment ? ` · ${latest.review_comment}` : ''}
                  </Text>
                )}
              </>
            )}
            {(analyses?.items.length ?? 0) > 1 && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                该分组共有 {analyses?.items.length} 次分析，当前展示最新一版（revision {latest.revision_no}）
              </Text>
            )}
          </Space>
        )}
      </Card>

      {/* -------------------------------------------------------------- 告警 */}
      <Card
        size="small"
        title="告警"
        extra={
          alert ? (
            <Button size="small" loading={busy === 'notify'} onClick={() => void notifyNow()}>
              补发通知
            </Button>
          ) : (
            <Button size="small" type="primary" loading={busy === 'alert'} onClick={() => void createAlert()}>
              建立告警
            </Button>
          )
        }
      >
        {!alert ? (
          <Empty description="该分组还没有告警" />
        ) : (
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label="严重级别">
                <Tag color={SEVERITY_COLOR[alert.severity] ?? 'default'}>{alert.severity}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="状态">
                <Tag>{ALERT_STATUS_LABEL[alert.status] ?? alert.status}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="首次出现">{time(alert.first_seen_at)}</Descriptions.Item>
              <Descriptions.Item label="最近出现">{time(alert.last_seen_at)}</Descriptions.Item>
              <Descriptions.Item label="累计出现次数">{alert.occurrence_count}</Descriptions.Item>
              <Descriptions.Item label="影响用例">{alert.failed_case_count}</Descriptions.Item>
              <Descriptions.Item label="影响执行次数">{alert.failed_run_count}</Descriptions.Item>
              <Descriptions.Item label="重新验证执行">
                {alert.reverify_run_id ? <Text code>{alert.reverify_run_id}</Text> : '-'}
              </Descriptions.Item>
            </Descriptions>
            {alert.severity_rule && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                定级规则 {alert.severity_rule.rule_id}：{alert.severity_rule.reason}
              </Text>
            )}
            <Space wrap>
              <Input
                size="small"
                placeholder="处理备注（可选）"
                style={{ width: 240 }}
                value={comment}
                onChange={(event) => setComment(event.target.value)}
              />
              {ALERT_ACTIONS.filter((item) => alert.allowed_transitions.includes(item.target)).map((item) => (
                <Button
                  key={item.target}
                  size="small"
                  danger={item.target === 'IGNORED'}
                  loading={busy === item.api}
                  onClick={() => void act(item.api, item.label)}
                >
                  {item.label}
                </Button>
              ))}
              {!alert.allowed_transitions.length && <Text type="secondary">该状态已无可用动作</Text>}
            </Space>
          </Space>
        )}
      </Card>

      {/* -------------------------------------------------------------- 通知 */}
      <Card size="small" title="通知记录">
        <Table<NotificationLog>
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={notifications}
          locale={{ emptyText: <Empty description={meta?.notification_enabled ? '还没有发送记录' : '企业微信未配置'} /> }}
          columns={[
            { title: '渠道', dataIndex: 'channel', width: 140 },
            {
              title: '状态',
              dataIndex: 'status',
              width: 100,
              render: (value: string) => (
                <Tag color={value === 'SENT' ? 'green' : value === 'FAILED' ? 'red' : 'default'}>{value}</Tag>
              ),
            },
            { title: '重试次数', dataIndex: 'attempt_count', width: 90 },
            { title: '发送时间', dataIndex: 'sent_at', width: 172, render: (value: string | null) => time(value) },
            {
              title: '失败原因',
              dataIndex: 'error_message',
              render: (value: string) => (value ? <Text type="danger">{value}</Text> : '-'),
            },
          ]}
        />
      </Card>
    </Space>
  )
}
