import { useCallback, useEffect, useState } from 'react'
import {
  Button, Card, Form, Input, InputNumber, Modal, Popconfirm, Radio, Select, Space, Switch, Table, Tag, Typography, message,
} from 'antd'
import { PlayCircleOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { client } from '../api/client'
import { useProjectScope } from '../hooks/useProjectScope'
import PageHeader, { ProjectPicker } from '../components/PageHeader'
import type { Environment, RegressionPlan, TestCase, TestSuite } from '../types'

export default function Plans() {
  const navigate = useNavigate()
  const { projects, projectId, setProjectId } = useProjectScope()
  const [rows, setRows] = useState<RegressionPlan[]>([])
  const [environments, setEnvironments] = useState<Environment[]>([])
  const [cases, setCases] = useState<TestCase[]>([])
  const [suites, setSuites] = useState<TestSuite[]>([])
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<RegressionPlan | null>(null)
  const [running, setRunning] = useState(false)
  const [sourceMode, setSourceMode] = useState<'suite' | 'cases'>('suite')
  const [form] = Form.useForm()

  const load = useCallback(async () => {
    if (!projectId) {
      setRows([])
      return
    }
    setLoading(true)
    try {
      const [plans, envs, caseList, suiteList] = await Promise.all([
        client.get<RegressionPlan[]>('/api/regression-plans', { params: { project_id: projectId } }),
        client.get<Environment[]>('/api/environments', { params: { project_id: projectId } }),
        client.get<TestCase[]>('/api/test-cases', { params: { project_id: projectId } }),
        client.get<TestSuite[]>('/api/test-suites', { params: { project_id: projectId } }),
      ])
      setRows(plans.data)
      setEnvironments(envs.data)
      setCases(caseList.data)
      setSuites(suiteList.data)
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    void load()
  }, [load])

  const openForm = (plan: RegressionPlan | null) => {
    setEditing(plan)
    form.resetFields()
    if (plan) {
      setSourceMode(plan.suite_id ? 'suite' : 'cases')
      form.setFieldsValue(plan)
    } else {
      setSourceMode(suites.length > 0 ? 'suite' : 'cases')
      form.setFieldsValue({
        browser: 'chromium',
        trigger_type: 'MANUAL',
        retry_count: 0,
        notification_enabled: false,
        enabled: true,
        environment_id: environments[0]?.id,
        suite_id: suites[0]?.id,
        case_ids: [],
      })
    }
    setOpen(true)
  }

  const submit = async () => {
    const values = await form.validateFields()
    const payload = {
      project_id: projectId,
      name: values.name,
      description: values.description ?? '',
      environment_id: values.environment_id,
      suite_id: sourceMode === 'suite' ? values.suite_id : null,
      case_ids: sourceMode === 'cases' ? values.case_ids ?? [] : [],
      browser: values.browser ?? 'chromium',
      trigger_type: values.trigger_type,
      schedule: values.schedule ?? '',
      retry_count: values.retry_count ?? 0,
      fail_retry: values.fail_retry ?? false,
      notification_enabled: false,
      enabled: values.enabled ?? true,
    }
    if (editing) await client.put(`/api/regression-plans/${editing.id}`, payload)
    else await client.post('/api/regression-plans', payload)
    message.success(editing ? '计划已更新' : '计划已创建')
    setOpen(false)
    await load()
  }

  const runNow = async (plan: RegressionPlan) => {
    if (!plan.environment_id) {
      message.warning('该计划未绑定环境，无法启动浏览器')
      return
    }
    setRunning(true)
    try {
      const { data } = await client.post('/api/runs', { plan_id: plan.id, trigger_source: 'MANUAL' })
      message.success('已触发执行，正在后台启动浏览器')
      navigate(`/runs/${data.id}`)
    } finally {
      setRunning(false)
    }
  }

  const envName = (id: string | null) => environments.find((item) => item.id === id)?.name ?? <Typography.Text type="danger">未绑定</Typography.Text>
  const selectedSuite = Form.useWatch('suite_id', form)

  return (
    <>
      <PageHeader
        title="回归计划"
        description="计划 = 环境 + 用例来源 + 触发方式。用例来源优先用测试套件；旧式直接选用例的方式仍保留可用。"
        picker={<ProjectPicker projects={projects} value={projectId} onChange={setProjectId} />}
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => void load()}>
              刷新
            </Button>
            <Button type="primary" icon={<PlusOutlined />} disabled={!projectId} onClick={() => openForm(null)}>
              新建计划
            </Button>
          </Space>
        }
      />
      <Card styles={{ body: { padding: 0 } }}>
        <Table<RegressionPlan>
          rowKey="id"
          loading={loading}
          dataSource={rows}
          pagination={false}
          locale={{ emptyText: projectId ? '还没有回归计划' : '请先选择项目' }}
          columns={[
            { title: '计划名称', dataIndex: 'name', render: (v: string) => <Typography.Text strong>{v}</Typography.Text> },
            {
              title: '用例来源',
              width: 220,
              render: (_, row) =>
                row.suite_id ? (
                  <Space size={6}>
                    <Tag color="purple">套件</Tag>
                    <Typography.Text>{row.suite_name || row.suite_id}</Typography.Text>
                  </Space>
                ) : (
                  <Tag>直接选用例</Tag>
                ),
            },
            { title: '用例数', width: 90, render: (_, row) => <Tag color="blue">{row.resolved_case_count}</Tag> },
            { title: '环境', width: 160, render: (_, row) => envName(row.environment_id) },
            { title: '重试次数', dataIndex: 'retry_count', width: 100 },
            {
              title: '触发方式',
              dataIndex: 'trigger_type',
              width: 110,
              render: (v: string) => <Tag color={v === 'MANUAL' ? 'blue' : v === 'CI' ? 'purple' : 'orange'}>{v}</Tag>,
            },
            {
              title: '启用',
              dataIndex: 'enabled',
              width: 80,
              render: (value: boolean) => (value ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>),
            },
            {
              title: '操作',
              width: 250,
              render: (_, row) => (
                <Space>
                  <Button size="small" type="primary" icon={<PlayCircleOutlined />} loading={running} onClick={() => void runNow(row)}>
                    执行
                  </Button>
                  <Button size="small" onClick={() => openForm(row)}>
                    编辑
                  </Button>
                  <Popconfirm
                    title="确认删除该计划？"
                    onConfirm={async () => {
                      await client.delete(`/api/regression-plans/${row.id}`)
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

      <Modal open={open} title={editing ? '编辑回归计划' : '新建回归计划'} onCancel={() => setOpen(false)} onOk={submit} width={660} destroyOnClose>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="计划名称" rules={[{ required: true, message: '请输入计划名称' }]}>
            <Input placeholder="Smoke" />
          </Form.Item>
          <Form.Item name="environment_id" label="执行环境" rules={[{ required: true, message: '请选择执行环境' }]}>
            <Select options={environments.map((item) => ({ label: `${item.name} (${item.base_url})`, value: item.id }))} />
          </Form.Item>

          <Form.Item label="用例来源">
            <Radio.Group value={sourceMode} onChange={(event) => setSourceMode(event.target.value)}>
              <Radio.Button value="suite">测试套件</Radio.Button>
              <Radio.Button value="cases">直接选用例</Radio.Button>
            </Radio.Group>
          </Form.Item>

          {sourceMode === 'suite' ? (
            <Form.Item name="suite_id" label="测试套件" rules={[{ required: true, message: '请选择测试套件' }]} extra="执行时按套件成员的顺序跑，停用的成员不参与">
              <Select
                placeholder={suites.length ? '选择套件' : '还没有套件，请先到「测试套件」创建'}
                options={suites.map((item) => ({ label: `${item.name}（启用 ${item.enabled_case_count} / 共 ${item.case_count}）`, value: item.id }))}
              />
            </Form.Item>
          ) : (
            <Form.Item name="case_ids" label="包含用例" rules={[{ required: true, message: '至少选择一个用例' }]}>
              <Select
                mode="multiple"
                optionFilterProp="label"
                options={cases.map((item) => ({ label: `${item.case_code || item.id} ${item.name}`, value: item.id }))}
              />
            </Form.Item>
          )}

          {sourceMode === 'suite' && !selectedSuite && suites.length > 0 && (
            <Typography.Text type="warning" style={{ fontSize: 12 }}>
              未选择套件时无法保存。
            </Typography.Text>
          )}

          <Space size="large" wrap style={{ marginTop: 8 }}>
            <Form.Item name="trigger_type" label="触发方式">
              <Select style={{ width: 140 }} options={['MANUAL', 'SCHEDULE', 'CI'].map((v) => ({ value: v, label: v }))} />
            </Form.Item>
            <Form.Item name="browser" label="浏览器">
              <Select style={{ width: 130 }} options={[{ value: 'chromium', label: 'chromium' }]} />
            </Form.Item>
            <Form.Item name="retry_count" label="失败重试次数" extra="每次重试都是真实重新执行">
              <InputNumber min={0} max={3} />
            </Form.Item>
            <Form.Item name="notification_enabled" label="企微通知" valuePropName="checked" extra="M5 启用">
              <Switch disabled />
            </Form.Item>
            <Form.Item name="enabled" label="启用" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </>
  )
}
