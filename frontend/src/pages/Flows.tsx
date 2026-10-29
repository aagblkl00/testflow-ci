import { useCallback, useEffect, useState } from 'react'
import {
  Button, Card, Drawer, Form, Input, InputNumber, Modal, Popconfirm, Select, Space, Switch, Table, Tag, Tooltip, Typography, message,
} from 'antd'
import { ArrowDownOutlined, ArrowUpOutlined, DeleteOutlined, PlusOutlined, ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { client } from '../api/client'
import { useProjectScope } from '../hooks/useProjectScope'
import PageHeader, { ProjectPicker } from '../components/PageHeader'
import type { AutomationFlow, Environment, FlowStep } from '../types'

interface FlowMeta {
  actions: { value: string; label: string }[]
  locator_priority: string[]
  demo_steps: FlowStep[]
}

const EMPTY_STEP: FlowStep = {
  name: '新步骤',
  action: 'click',
  locator_type: 'testid',
  locator_value: '',
  value: '',
  expected: '',
  timeout: 15000,
  retries: 0,
  screenshot: false,
  continue_on_failure: false,
  disabled: false,
}

export default function Flows() {
  const { projects, projectId, setProjectId } = useProjectScope()
  const [rows, setRows] = useState<AutomationFlow[]>([])
  const [environments, setEnvironments] = useState<Environment[]>([])
  const [meta, setMeta] = useState<FlowMeta | null>(null)
  const [loading, setLoading] = useState(false)

  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<AutomationFlow | null>(null)
  const [form] = Form.useForm()

  const [drawerOpen, setDrawerOpen] = useState(false)
  const [activeFlow, setActiveFlow] = useState<AutomationFlow | null>(null)
  const [steps, setSteps] = useState<FlowStep[]>([])

  const load = useCallback(async () => {
    if (!projectId) {
      setRows([])
      return
    }
    setLoading(true)
    try {
      const [flows, envs, flowMeta] = await Promise.all([
        client.get<AutomationFlow[]>('/api/flows', { params: { project_id: projectId } }),
        client.get<Environment[]>('/api/environments', { params: { project_id: projectId } }),
        client.get<FlowMeta>('/api/flows/meta'),
      ])
      setRows(flows.data)
      setEnvironments(envs.data)
      setMeta(flowMeta.data)
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    void load()
  }, [load])

  const saveFlow = async () => {
    const values = await form.validateFields()
    const payload = {
      project_id: projectId,
      name: values.name,
      engine: 'playwright',
      environment_id: values.environment_id ?? '',
      variables: {},
      steps: editing ? (editing.definition?.steps ?? []) : (meta?.demo_steps ?? []),
    }
    if (editing) await client.put(`/api/flows/${editing.id}`, payload)
    else await client.post('/api/flows', payload)
    message.success(editing ? '流程已更新' : '流程已创建')
    setFormOpen(false)
    setEditing(null)
    await load()
  }

  const saveSteps = async () => {
    if (!activeFlow) return
    await client.put(`/api/flows/${activeFlow.id}`, {
      project_id: activeFlow.project_id,
      name: activeFlow.name,
      engine: activeFlow.engine,
      environment_id: activeFlow.environment_id,
      variables: activeFlow.variables ?? {},
      steps,
    })
    message.success('步骤已保存')
    setDrawerOpen(false)
    await load()
  }

  const patchStep = (index: number, patch: Partial<FlowStep>) => {
    setSteps((prev) => prev.map((item, i) => (i === index ? { ...item, ...patch } : item)))
  }

  const moveStep = (index: number, delta: number) => {
    setSteps((prev) => {
      const next = [...prev]
      const target = index + delta
      if (target < 0 || target >= next.length) return prev
      ;[next[index], next[target]] = [next[target], next[index]]
      return next
    })
  }

  const actionLabel = (action: string) => meta?.actions.find((item) => item.value === action)?.label ?? action

  return (
    <>
      <PageHeader
        title="自动化流程"
        description="流程就是真正被 Playwright 执行的步骤序列。变量用 ${name} 引用，运行时从环境/流程变量注入。"
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
                form.setFieldsValue({ environment_id: environments[0]?.id })
                setFormOpen(true)
              }}
            >
              新建流程
            </Button>
          </Space>
        }
      />
      <Card styles={{ body: { padding: 0 } }}>
        <Table<AutomationFlow>
          rowKey="id"
          loading={loading}
          dataSource={rows}
          pagination={false}
          locale={{ emptyText: projectId ? '还没有自动化流程' : '请先选择项目' }}
          columns={[
            { title: '流程名称', dataIndex: 'name', render: (v: string) => <Typography.Text strong>{v}</Typography.Text> },
            { title: '版本', dataIndex: 'version', width: 80, render: (v: number) => <Tag>v{v}</Tag> },
            {
              title: '步骤数',
              width: 90,
              render: (_, row) => row.definition?.steps?.length ?? 0,
            },
            {
              title: '步骤概览',
              render: (_, row) => (
                <Space size={4} wrap>
                  {(row.definition?.steps ?? []).slice(0, 6).map((step, index) => (
                    <Tag key={index}>{actionLabel(step.action)}</Tag>
                  ))}
                  {(row.definition?.steps?.length ?? 0) > 6 && <Tag>…</Tag>}
                </Space>
              ),
            },
            {
              title: '操作',
              width: 230,
              render: (_, row) => (
                <Space>
                  <Button
                    size="small"
                    type="primary"
                    ghost
                    icon={<ThunderboltOutlined />}
                    onClick={() => {
                      setActiveFlow(row)
                      setSteps((row.definition?.steps ?? []).map((item) => ({ ...EMPTY_STEP, ...item })))
                      setDrawerOpen(true)
                    }}
                  >
                    编辑步骤
                  </Button>
                  <Button
                    size="small"
                    onClick={() => {
                      setEditing(row)
                      form.setFieldsValue(row)
                      setFormOpen(true)
                    }}
                  >
                    重命名
                  </Button>
                  <Popconfirm
                    title="确认删除该流程？绑定关系会一并删除。"
                    onConfirm={async () => {
                      await client.delete(`/api/flows/${row.id}`)
                      await load()
                    }}
                  >
                    <Button size="small" danger>
                      删除
                    </Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal open={formOpen} title={editing ? '编辑流程' : '新建流程'} onCancel={() => setFormOpen(false)} onOk={saveFlow} destroyOnClose>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="流程名称" rules={[{ required: true, message: '请输入流程名称' }]}>
            <Input placeholder="FLOW-LOGIN-001" />
          </Form.Item>
          <Form.Item name="environment_id" label="默认环境" extra="可留空，执行时以回归计划绑定的环境为准">
            <Select
              allowClear
              placeholder="选择环境"
              options={environments.map((item) => ({ label: `${item.name} (${item.base_url})`, value: item.id }))}
            />
          </Form.Item>
          {!editing && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              新建时会自动填入一套「登录冒烟」示例步骤，可直接改。
            </Typography.Text>
          )}
        </Form>
      </Modal>

      <Drawer
        open={drawerOpen}
        width={980}
        title={`编辑步骤 · ${activeFlow?.name ?? ''}`}
        onClose={() => setDrawerOpen(false)}
        extra={
          <Space>
            <Button onClick={() => setSteps((meta?.demo_steps ?? []).map((item) => ({ ...EMPTY_STEP, ...item })))}>载入登录示例</Button>
            <Button onClick={() => setSteps((prev) => [...prev, { ...EMPTY_STEP }])} icon={<PlusOutlined />}>
              新增步骤
            </Button>
            <Button type="primary" onClick={saveSteps}>
              保存
            </Button>
          </Space>
        }
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          定位优先级（方案 §5）：role &gt; label &gt; test_id &gt; CSS &gt; XPath。变量写法 <Typography.Text code>${'{'}base_url{'}'}</Typography.Text>。
        </Typography.Paragraph>
        {steps.length === 0 && <Typography.Text type="secondary">还没有步骤，点右上角「新增步骤」。</Typography.Text>}
        {steps.map((step, index) => (
          <Card
            key={index}
            size="small"
            style={{ marginBottom: 10 }}
            title={
              <Space>
                <Tag color="blue">{index + 1}</Tag>
                <Input
                  size="small"
                  style={{ width: 240 }}
                  value={step.name}
                  onChange={(event) => patchStep(index, { name: event.target.value })}
                />
                <Select
                  size="small"
                  style={{ width: 150 }}
                  value={step.action}
                  onChange={(value) => patchStep(index, { action: value })}
                  options={meta?.actions ?? []}
                />
              </Space>
            }
            extra={
              <Space>
                <Tooltip title="上移">
                  <Button size="small" icon={<ArrowUpOutlined />} onClick={() => moveStep(index, -1)} />
                </Tooltip>
                <Tooltip title="下移">
                  <Button size="small" icon={<ArrowDownOutlined />} onClick={() => moveStep(index, 1)} />
                </Tooltip>
                <Tooltip title="删除">
                  <Button size="small" danger icon={<DeleteOutlined />} onClick={() => setSteps((prev) => prev.filter((_, i) => i !== index))} />
                </Tooltip>
              </Space>
            }
          >
            <Space wrap size="middle">
              <div>
                <div style={{ fontSize: 12, color: '#8c8c8c' }}>定位方式</div>
                <Select
                  size="small"
                  style={{ width: 130 }}
                  value={step.locator_type || 'css'}
                  onChange={(value) => patchStep(index, { locator_type: value })}
                  options={(meta?.locator_priority ?? ['css']).map((item) => ({ value: item, label: item }))}
                />
              </div>
              <div>
                <div style={{ fontSize: 12, color: '#8c8c8c' }}>定位值</div>
                <Input
                  size="small"
                  style={{ width: 210 }}
                  placeholder="login-submit / 用户名 / #submit"
                  value={step.locator_value}
                  onChange={(event) => patchStep(index, { locator_value: event.target.value })}
                />
              </div>
              <div>
                <div style={{ fontSize: 12, color: '#8c8c8c' }}>输入值 / 属性名</div>
                <Input
                  size="small"
                  style={{ width: 190 }}
                  placeholder="${username}"
                  value={step.value}
                  onChange={(event) => patchStep(index, { value: event.target.value })}
                />
              </div>
              <div>
                <div style={{ fontSize: 12, color: '#8c8c8c' }}>期望值</div>
                <Input
                  size="small"
                  style={{ width: 190 }}
                  placeholder="断言用"
                  value={step.expected}
                  onChange={(event) => patchStep(index, { expected: event.target.value })}
                />
              </div>
              <div>
                <div style={{ fontSize: 12, color: '#8c8c8c' }}>超时(ms)</div>
                <InputNumber
                  size="small"
                  style={{ width: 110 }}
                  min={500}
                  step={500}
                  value={step.timeout}
                  onChange={(value) => patchStep(index, { timeout: Number(value) || 15000 })}
                />
              </div>
              <div>
                <div style={{ fontSize: 12, color: '#8c8c8c' }}>重试</div>
                <InputNumber
                  size="small"
                  style={{ width: 80 }}
                  min={0}
                  max={3}
                  value={step.retries}
                  onChange={(value) => patchStep(index, { retries: Number(value) || 0 })}
                />
              </div>
              <div>
                <div style={{ fontSize: 12, color: '#8c8c8c' }}>截图</div>
                <Switch size="small" checked={step.screenshot} onChange={(value) => patchStep(index, { screenshot: value })} />
              </div>
            </Space>
          </Card>
        ))}
      </Drawer>
    </>
  )
}
