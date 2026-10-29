import { useCallback, useEffect, useState } from 'react'
import {
  Button, Card, Drawer, Form, Input, Modal, Popconfirm, Select, Space, Switch, Table, Tag, Tooltip, Typography, message,
} from 'antd'
import { ArrowDownOutlined, ArrowUpOutlined, DeleteOutlined, PlusOutlined, ReloadOutlined, TeamOutlined } from '@ant-design/icons'
import { client } from '../api/client'
import { useProjectScope } from '../hooks/useProjectScope'
import PageHeader, { ProjectPicker } from '../components/PageHeader'
import type { SuiteMember, TestCase, TestSuite } from '../types'

export default function Suites() {
  const { projects, projectId, setProjectId } = useProjectScope()
  const [rows, setRows] = useState<TestSuite[]>([])
  const [cases, setCases] = useState<TestCase[]>([])
  const [loading, setLoading] = useState(false)

  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<TestSuite | null>(null)
  const [form] = Form.useForm()

  const [drawerOpen, setDrawerOpen] = useState(false)
  const [active, setActive] = useState<TestSuite | null>(null)
  const [members, setMembers] = useState<SuiteMember[]>([])

  const load = useCallback(async () => {
    if (!projectId) {
      setRows([])
      return
    }
    setLoading(true)
    try {
      const [suites, caseList] = await Promise.all([
        client.get<TestSuite[]>('/api/test-suites', { params: { project_id: projectId } }),
        client.get<TestCase[]>('/api/test-cases', { params: { project_id: projectId } }),
      ])
      setRows(suites.data)
      setCases(caseList.data)
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    void load()
  }, [load])

  const submit = async () => {
    const values = await form.validateFields()
    const payload = {
      project_id: projectId,
      name: values.name,
      description: values.description ?? '',
      enabled: values.enabled ?? true,
      case_ids: values.case_ids ?? [],
    }
    if (editing) await client.put(`/api/test-suites/${editing.id}`, payload)
    else await client.post('/api/test-suites', payload)
    message.success(editing ? '套件已更新' : '套件已创建')
    setFormOpen(false)
    setEditing(null)
    await load()
  }

  const patchMember = (caseId: string, patch: Partial<SuiteMember>) => {
    setMembers((prev) => prev.map((item) => (item.case_id === caseId ? { ...item, ...patch } : item)))
  }

  const moveMember = (index: number, delta: number) => {
    setMembers((prev) => {
      const next = [...prev]
      const target = index + delta
      if (target < 0 || target >= next.length) return prev
      ;[next[index], next[target]] = [next[target], next[index]]
      return next
    })
  }

  const saveMembers = async () => {
    if (!active) return
    const { data } = await client.put(`/api/test-suites/${active.id}/order`, {
      items: members.map((item, index) => ({ case_id: item.case_id, sort_order: index + 1, enabled: item.enabled })),
    })
    setMembers(data.members)
    message.success('套件成员已保存')
    await load()
  }

  const removeMember = async (caseId: string) => {
    if (!active) return
    const { data } = await client.delete(`/api/test-suites/${active.id}/cases/${caseId}`)
    setMembers(data.members)
    await load()
  }

  return (
    <>
      <PageHeader
        title="测试套件"
        description="把用例组织成可复用的集合（Smoke / 登录回归 / 订单回归…）。回归计划优先引用套件，成员顺序就是执行顺序，可以单独停用某个用例。"
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
                form.setFieldsValue({ enabled: true, case_ids: [] })
                setFormOpen(true)
              }}
            >
              新建套件
            </Button>
          </Space>
        }
      />
      <Card styles={{ body: { padding: 0 } }}>
        <Table<TestSuite>
          rowKey="id"
          loading={loading}
          dataSource={rows}
          pagination={false}
          locale={{ emptyText: projectId ? '还没有测试套件' : '请先选择项目' }}
          columns={[
            { title: '套件名称', dataIndex: 'name', render: (v: string) => <Typography.Text strong>{v}</Typography.Text> },
            { title: '说明', dataIndex: 'description', render: (v: string) => v || '-' },
            {
              title: '用例数',
              width: 140,
              render: (_, row) => (
                <Space size={6}>
                  <Tag color="blue">启用 {row.enabled_case_count}</Tag>
                  {row.case_count > row.enabled_case_count && <Tag>停用 {row.case_count - row.enabled_case_count}</Tag>}
                </Space>
              ),
            },
            {
              title: '状态',
              dataIndex: 'enabled',
              width: 90,
              render: (value: boolean) => (value ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>),
            },
            {
              title: '操作',
              width: 260,
              render: (_, row) => (
                <Space>
                  <Button
                    size="small"
                    type="primary"
                    ghost
                    icon={<TeamOutlined />}
                    onClick={() => {
                      setActive(row)
                      setMembers(row.members.map((item) => ({ ...item })))
                      setDrawerOpen(true)
                    }}
                  >
                    成员
                  </Button>
                  <Button
                    size="small"
                    onClick={() => {
                      setEditing(row)
                      form.setFieldsValue({ ...row, case_ids: row.members.map((item) => item.case_id) })
                      setFormOpen(true)
                    }}
                  >
                    编辑
                  </Button>
                  <Popconfirm
                    title="确认删除该套件？引用它的回归计划需要重新选择。"
                    onConfirm={async () => {
                      await client.delete(`/api/test-suites/${row.id}`)
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

      <Modal open={formOpen} title={editing ? '编辑套件' : '新建套件'} onCancel={() => setFormOpen(false)} onOk={submit} width={640} destroyOnClose>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="套件名称" rules={[{ required: true, message: '请输入套件名称' }]}>
            <Input placeholder="Smoke" />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input.TextArea rows={2} />
          </Form.Item>
          <Form.Item name="case_ids" label="包含用例" extra="顺序即执行顺序，创建后可在「成员」里调整">
            <Select mode="multiple" optionFilterProp="label" options={cases.map((item) => ({ label: `${item.case_code || item.id} ${item.name}`, value: item.id }))} />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        open={drawerOpen}
        width={860}
        title={`套件成员 · ${active?.name ?? ''}`}
        onClose={() => setDrawerOpen(false)}
        extra={
          <Button type="primary" onClick={saveMembers}>
            保存顺序与启停
          </Button>
        }
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          顺序即执行顺序；停用的用例不会参与执行，但仍保留在套件里。
        </Typography.Paragraph>
        {members.length === 0 && <Typography.Text type="secondary">该套件还没有成员。</Typography.Text>}
        <Table<SuiteMember>
          rowKey="case_id"
          size="small"
          pagination={false}
          dataSource={members}
          columns={[
            { title: '#', width: 50, render: (_, __, index) => index + 1 },
            { title: '编号', dataIndex: 'case_code', width: 150 },
            { title: '名称', dataIndex: 'case_name' },
            { title: '模块', dataIndex: 'module', width: 100 },
            {
              title: '绑定流程',
              width: 150,
              render: (_, row) => (row.flow_name ? <Tag color="green">{row.flow_name}</Tag> : <Tag color="red">未绑定</Tag>),
            },
            {
              title: '启用',
              width: 80,
              render: (_, row, index) => (
                <Switch size="small" checked={row.enabled} onChange={(value) => patchMember(row.case_id, { enabled: value })} />
              ),
            },
            {
              title: '操作',
              width: 120,
              render: (_, row, index) => (
                <Space>
                  <Tooltip title="上移">
                    <Button size="small" icon={<ArrowUpOutlined />} onClick={() => moveMember(index, -1)} />
                  </Tooltip>
                  <Tooltip title="下移">
                    <Button size="small" icon={<ArrowDownOutlined />} onClick={() => moveMember(index, 1)} />
                  </Tooltip>
                  <Tooltip title="移出套件">
                    <Button size="small" danger icon={<DeleteOutlined />} onClick={() => void removeMember(row.case_id)} />
                  </Tooltip>
                </Space>
              ),
            },
          ]}
        />
      </Drawer>
    </>
  )
}
