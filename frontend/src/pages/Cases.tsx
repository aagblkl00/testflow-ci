import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Form, Input, Modal, Popconfirm, Select, Space, Table, Tag, Typography, message } from 'antd'
import { CopyOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { client } from '../api/client'
import { useProjectScope } from '../hooks/useProjectScope'
import PageHeader, { ProjectPicker } from '../components/PageHeader'
import type { AutomationFlow, TestCase } from '../types'

const PRIORITY_COLOR: Record<string, string> = { P0: 'red', P1: 'orange', P2: 'blue', P3: 'default' }

export default function Cases() {
  const { projects, projectId, setProjectId } = useProjectScope()
  const [rows, setRows] = useState<TestCase[]>([])
  const [flows, setFlows] = useState<AutomationFlow[]>([])
  const [loading, setLoading] = useState(false)
  const [keyword, setKeyword] = useState('')
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<TestCase | null>(null)
  const [form] = Form.useForm()

  const load = useCallback(async () => {
    if (!projectId) {
      setRows([])
      return
    }
    setLoading(true)
    try {
      const [cases, flowList] = await Promise.all([
        client.get<TestCase[]>('/api/test-cases', { params: { project_id: projectId, q: keyword } }),
        client.get<AutomationFlow[]>('/api/flows', { params: { project_id: projectId } }),
      ])
      setRows(cases.data)
      setFlows(flowList.data)
    } finally {
      setLoading(false)
    }
  }, [projectId, keyword])

  useEffect(() => {
    void load()
  }, [load])

  const bind = async (caseId: string, flowId: string) => {
    await client.post('/api/case-flow-bindings', { case_id: caseId, flow_id: flowId, enabled: true })
    message.success('已绑定自动化流程')
    await load()
  }

  const submit = async () => {
    const values = await form.validateFields()
    const payload = {
      project_id: projectId,
      name: values.name,
      case_code: values.case_code ?? '',
      module: values.module ?? '',
      priority: values.priority,
      status: values.status,
      tags: String(values.tags ?? '')
        .split(',')
        .map((item: string) => item.trim())
        .filter(Boolean),
      description: values.description ?? '',
      preconditions: values.preconditions ?? '',
      expected: values.expected ?? '',
      created_by: values.created_by ?? '',
      steps: [],
    }
    if (editing) await client.put(`/api/test-cases/${editing.id}`, payload)
    else await client.post('/api/test-cases', payload)
    message.success(editing ? '用例已更新' : '用例已创建')
    setOpen(false)
    setEditing(null)
    await load()
  }

  return (
    <>
      <PageHeader
        title="测试用例"
        description="用例与自动化流程通过「绑定」关联，不使用硬编码字段；执行时按绑定关系找到流程。"
        picker={<ProjectPicker projects={projects} value={projectId} onChange={setProjectId} />}
        extra={
          <Space>
            <Input.Search
              allowClear
              placeholder="搜索编号 / 名称 / 模块"
              style={{ width: 240 }}
              onSearch={setKeyword}
            />
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
                form.setFieldsValue({ priority: 'P2', status: 'ACTIVE' })
                setOpen(true)
              }}
            >
              新建用例
            </Button>
          </Space>
        }
      />
      <Card styles={{ body: { padding: 0 } }}>
        <Table<TestCase>
          rowKey="id"
          loading={loading}
          dataSource={rows}
          pagination={{ pageSize: 20, hideOnSinglePage: true }}
          locale={{ emptyText: projectId ? '还没有测试用例' : '请先选择项目' }}
          columns={[
            {
              title: '编号',
              dataIndex: 'case_code',
              width: 150,
              render: (value: string) => value || '-',
            },
            { title: '名称', dataIndex: 'name', render: (value: string, row) => <Typography.Text strong>{row.name}</Typography.Text> },
            { title: '模块', dataIndex: 'module', width: 120, render: (v: string) => v || '-' },
            {
              title: '优先级',
              dataIndex: 'priority',
              width: 90,
              render: (value: string) => <Tag color={PRIORITY_COLOR[value] ?? 'default'}>{value}</Tag>,
            },
            {
              title: '标签',
              dataIndex: 'tags',
              width: 140,
              render: (value: string[]) => (value?.length ? value.map((item) => <Tag key={item}>{item}</Tag>) : '-'),
            },
            {
              title: '绑定流程',
              width: 220,
              render: (_, row) =>
                row.flow_id ? (
                  <Space>
                    <Tag color="green">{row.flow_name}</Tag>
                    <Popconfirm
                      title="解除绑定？"
                      onConfirm={async () => {
                        const { data } = await client.get('/api/case-flow-bindings', { params: { case_id: row.id } })
                        await Promise.all(data.map((item: { id: string }) => client.delete(`/api/case-flow-bindings/${item.id}`)))
                        await load()
                      }}
                    >
                      <Button size="small" type="link" danger>
                        解绑
                      </Button>
                    </Popconfirm>
                  </Space>
                ) : (
                  <Select
                    size="small"
                    style={{ width: 180 }}
                    placeholder="选择流程绑定"
                    value={undefined}
                    onChange={(value: string) => void bind(row.id, value)}
                    options={flows.map((item) => ({ label: item.name, value: item.id }))}
                    notFoundContent="请先在「自动化流程」创建流程"
                  />
                ),
            },
            {
              title: '操作',
              width: 170,
              render: (_, row) => (
                <Space>
                  <Button
                    size="small"
                    onClick={() => {
                      setEditing(row)
                      form.setFieldsValue({ ...row, tags: (row.tags ?? []).join(',') })
                      setOpen(true)
                    }}
                  >
                    编辑
                  </Button>
                  <Button size="small" icon={<CopyOutlined />} onClick={async () => { await client.post(`/api/test-cases/${row.id}/duplicate`); await load() }}>
                    复制
                  </Button>
                  <Popconfirm
                    title="确认删除该用例？"
                    onConfirm={async () => {
                      await client.delete(`/api/test-cases/${row.id}`)
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

      <Modal open={open} title={editing ? '编辑测试用例' : '新建测试用例'} onCancel={() => setOpen(false)} onOk={submit} width={640} destroyOnClose>
        <Form form={form} layout="vertical">
          <Space size="middle" style={{ width: '100%' }}>
            <Form.Item name="case_code" label="用例编号" style={{ width: 200 }}>
              <Input placeholder="TC-LOGIN-001" />
            </Form.Item>
            <Form.Item name="module" label="所属模块" style={{ width: 160 }}>
              <Input placeholder="登录模块" />
            </Form.Item>
            <Form.Item name="priority" label="优先级" style={{ width: 110 }}>
              <Select options={['P0', 'P1', 'P2', 'P3'].map((v) => ({ value: v, label: v }))} />
            </Form.Item>
            <Form.Item name="status" label="状态" style={{ width: 120 }}>
              <Select options={['ACTIVE', 'DRAFT', 'DISABLED'].map((v) => ({ value: v, label: v }))} />
            </Form.Item>
          </Space>
          <Form.Item name="name" label="用例名称" rules={[{ required: true, message: '请输入用例名称' }]}>
            <Input placeholder="登录冒烟：账号密码登录并进入首页" />
          </Form.Item>
          <Form.Item name="tags" label="标签" extra="逗号分隔">
            <Input placeholder="smoke,登录" />
          </Form.Item>
          <Form.Item name="preconditions" label="前置条件">
            <Input.TextArea rows={2} placeholder="Demo 站可访问，账号 demo/demo123 可用" />
          </Form.Item>
          <Form.Item name="expected" label="预期结果">
            <Input.TextArea rows={2} placeholder="流程执行 PASSED" />
          </Form.Item>
          <Form.Item name="description" label="备注">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
