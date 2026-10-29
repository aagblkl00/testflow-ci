import type { ReactNode } from 'react'
import { Select, Space, Typography } from 'antd'
import type { Project } from '../types'

export function ProjectPicker({
  projects,
  value,
  onChange,
}: {
  projects: Project[]
  value: string
  onChange: (value: string) => void
}) {
  return (
    <Select
      style={{ width: 220 }}
      value={value || undefined}
      placeholder="选择项目"
      onChange={onChange}
      options={projects.map((item) => ({ label: item.name, value: item.id }))}
    />
  )
}

export default function PageHeader({
  title,
  description,
  extra,
  picker,
}: {
  title: string
  description?: string
  extra?: ReactNode
  picker?: ReactNode
}) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'space-between',
        marginBottom: 16,
        gap: 16,
        flexWrap: 'wrap',
      }}
    >
      <div>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {title}
        </Typography.Title>
        {description && (
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            {description}
          </Typography.Text>
        )}
      </div>
      <Space wrap>
        {picker}
        {extra}
      </Space>
    </div>
  )
}
