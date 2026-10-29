import { Tag } from 'antd'

const COLOR: Record<string, string> = {
  PASSED: 'green',
  FAILED: 'red',
  ERROR: 'volcano',
  SKIPPED: 'default',
  FLAKY: 'gold',
  PARTIAL: 'orange',
  CANCELLED: 'purple',
  CANCELLING: 'magenta',
  QUEUED: 'blue',
  RUNNING: 'processing',
  PENDING: 'default',
  // M3 失败分组状态
  OPEN: 'red',
  RESOLVED: 'green',
  IGNORED: 'default',
}

/** 分组状态给中文，执行状态保持原文（和后端状态词表一一对应，便于排查）。 */
const LABEL: Record<string, string> = {
  OPEN: '待处理',
  RESOLVED: '已解决',
  IGNORED: '已忽略',
}

export default function StatusTag({ status }: { status: string }) {
  return <Tag color={COLOR[status] ?? 'default'}>{LABEL[status] ?? status}</Tag>
}
