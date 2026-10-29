import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Form, Input, Modal, Popconfirm, Select, Space, Switch, Table, Tag, Typography, message } from 'antd'
import { DeleteOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { client } from '../api/client'
import { useProjectScope } from '../hooks/useProjectScope'
import PageHeader, { ProjectPicker } from '../components/PageHeader'
import type { Environment } from '../types'

const TYPE_COLOR: Record<string, string> = { DEV: 'default', TEST: 'blue', STAGING: 'orange', PROD: 'red' }

function varsToText(variables: Record<string, string>): string {
  return Object.entries(variables ?? {})
    .map(([key, value]) => `${key}=${value}`)
    .join('\n')
}

function textToVars(text: string): Record<string, string> {
  const result: Record<string, string> = {}
  for (const raw of (text ?? '').split('\n')) {
    const line = raw.trim()
    if (!line || line.startsWith('#')) continue
    const index = line.indexOf('=')
    if (index <= 0) continue
    result[line.slice(0, index).trim()] = line.slice(index + 1).trim()
  }
  return result
}

export default function Environments() {
  const { projects, projectId, setProjectId } = useProjectScope()
  const [rows, setRows] = useState<Environment[]>([])
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<Environment | null>(null)
  const [form] = Form.useForm()

  const load = useCallback(async () => {
    if (!projectId) {
      setRows([])
      return
    }
    setLoading(true)
    try {
      const { data } = await client.get<Environment[]>('/api/environments', { params: { project_id: projectId } })
      setRows(data)
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    void load()
  }, [load])

  const remove = async (row: Environment) => {
    try {
      await client.delete(`/api/environments/${row.id}`)
      message.success(`已删除环境「${row.name}」`)
      void load()
    } catch {
      message.error('删除失败')
    }
  }

  const submit = async () => {
    const values = await form.validateFields()
    const payload = {
      project_id: projectId,
      name: values.name,
      base_url: values.base_url,
      browser: values.browser,
      environment_type: values.environment_type,
      allowed_hosts: String(values.allowed_hosts ?? '')
        .split(',')
        .map((item: string) => item.trim())
        .filter(Boolean),
      read_only: !!values.read_only,
      variables: textToVars(values.variables_text ?? ''),
    }
    if (editing) await client.put(`/api/environments/${editing.id}`, payload)
    else await client.post('/api/environments', payload)
    message.success(editing ? '环境已更新' : '环境已创建')
    setOpen(false)
    setEditing(null)
    await load()
  }

  return (
    <>
      <PageHeader
        title="环境"
        description="环境的 Base URL 必须登记到域名白名单，执行时才不会被安全门禁拦下；PROD 默认禁止执行。"
        picker={<ProjectPicker projects={projects} value={projectId} onChange={setProjectId} />}
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => void load()}>
              刷新
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              disabled={!projectId}
              onClick={() => {
                setEditing(null)
                form.resetFields()
                form.setFieldsValue({ browser: 'chromium', environment_type: 'TEST', read_only: false, variables_text: 'username=\npassword=' })
                setOpen(true)
              }}
            >
              新建环境
            </Button>
          </Space>
        }
      />
      <Card styles={{ body: { padding: 0 } }}>
        <Table<Environment>
          rowKey="id"
          loading={loading}
          dataSource={rows}
          pagination={false}
          locale={{ emptyText: projectId ? '该项目还没有环境' : '请先选择项目' }}
          columns={[
            { title: '名称', dataIndex: 'name' },
            {
              title: '类型',
              dataIndex: 'environment_type',
              width: 100,
              render: (value: string) => <Tag color={TYPE_COLOR[value] ?? 'default'}>{value}</Tag>,
            },
            { title: 'Base URL', dataIndex: 'base_url', render: (v: string) => <Typography.Text code>{v}</Typography.Text> },
            {
              title: '白名单',
              dataIndex: 'allowed_hosts',
              render: (value: string[]) => (value?.length ? value.join(', ') : <Typography.Text type="danger">未登记（会被拦截）</Typography.Text>),
            },
            { title: '浏览器', dataIndex: 'browser', width: 100 },
            {
              title: '只读',
              dataIndex: 'read_only',
              width: 80,
              render: (value: boolean) => (value ? <Tag>只读</Tag> : '-'),
            },
            {
              title: '变量（已脱敏）',
              dataIndex: 'variables',
              render: (value: Record<string, string>) =>
                Object.entries(value ?? {}).length === 0
                  ? '-'
                  : Object.entries(value).map(([key, item]) => (
                      <Tag key={key}>
                        {key}={item}
                      </Tag>
                    )),
            },
            {
              title: '操作',
              width: 150,
              render: (_, row) => (
                <Space size={4}>
                  <Button
                    size="small"
                    onClick={() => {
                      setEditing(row)
                      form.setFieldsValue({
                        ...row,
                        allowed_hosts: (row.allowed_hosts ?? []).join(','),
                        variables_text: varsToText(row.variables),
                      })
                      setOpen(true)
                    }}
                  >
                    编辑
                  </Button>
                  <Popconfirm
                    title="删除环境"
                    description={
                      <>
                        确认删除「{row.name}」？
                        <br />
                        引用它的回归计划会把环境置空，需要重新选择后才能执行。
                      </>
                    }
                    okText="删除"
                    okButtonProps={{ danger: true }}
                    cancelText="取消"
                    onConfirm={() => void remove(row)}
                  >
                    <Button size="small" danger icon={<DeleteOutlined />}>
                      删除
                    </Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal open={open} title={editing ? '编辑环境' : '新建环境'} onCancel={() => setOpen(false)} onOk={submit} width={620} destroyOnClose>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="环境名称" rules={[{ required: true, message: '请输入环境名称' }]}>
            <Input placeholder="例如：本地 Demo 测试环境" />
          </Form.Item>
          <Form.Item
            name="base_url"
            label="Base URL"
            rules={[{ required: true, message: '请输入 Base URL' }]}
            extra="流程里的 ${base_url} 会用这个值"
          >
            <Input placeholder="http://127.0.0.1:8100" />
          </Form.Item>
          <Space style={{ width: '100%' }} size="middle">
            <Form.Item name="environment_type" label="环境类型" style={{ width: 160 }}>
              <Select options={['DEV', 'TEST', 'STAGING', 'PROD'].map((v) => ({ value: v, label: v }))} />
            </Form.Item>
            <Form.Item name="browser" label="浏览器" style={{ width: 160 }}>
              <Select options={[{ value: 'chromium', label: 'chromium' }]} />
            </Form.Item>
            <Form.Item name="read_only" label="只读模式" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>
          <Form.Item
            name="allowed_hosts"
            label="域名白名单"
            rules={[{ required: true, message: '至少填一个域名' }]}
            extra="逗号分隔。不登记会被安全门禁拦截，执行直接报 ERROR。"
          >
            <Input placeholder="127.0.0.1,localhost" />
          </Form.Item>
          <Form.Item
            name="variables_text"
            label="环境变量"
            extra="每行一个 key=value。password / token / secret 等敏感变量在接口、日志、通知里都会显示为 ********"
          >
            <Input.TextArea rows={5} placeholder={'username=demo\npassword=demo123'} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
