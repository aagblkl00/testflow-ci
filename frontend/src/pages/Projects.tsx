import { useState } from 'react'
import { Button, Card, Form, Input, Modal, Popconfirm, Space, Table, Typography, message } from 'antd'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { client } from '../api/client'
import { useProjectScope } from '../hooks/useProjectScope'
import PageHeader from '../components/PageHeader'
import type { Project } from '../types'

export default function Projects() {
  const { projects, loading, reload, setProjectId, projectId } = useProjectScope()
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<Project | null>(null)
  const [form] = Form.useForm()

  const submit = async () => {
    const values = await form.validateFields()
    if (editing) await client.put(`/api/projects/${editing.id}`, values)
    else await client.post('/api/projects', values)
    message.success(editing ? '项目已更新' : '项目已创建')
    setOpen(false)
    setEditing(null)
    form.resetFields()
    await reload()
  }

  return (
    <>
      <PageHeader
        title="项目"
        description="一个项目对应一套被测系统，后续的环境、用例、流程、计划都挂在项目下。"
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => void reload()}>
              刷新
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => {
                setEditing(null)
                form.resetFields()
                setOpen(true)
              }}
            >
              新建项目
            </Button>
          </Space>
        }
      />
      <Card styles={{ body: { padding: 0 } }}>
        <Table<Project>
          rowKey="id"
          loading={loading}
          dataSource={projects}
          pagination={false}
          locale={{ emptyText: '还没有项目，点右上角「新建项目」开始' }}
          columns={[
            {
              title: '名称',
              dataIndex: 'name',
              render: (value: string, row) => (
                <Space>
                  <Typography.Text strong>{value}</Typography.Text>
                  {row.id === projectId && <Typography.Text type="secondary">当前</Typography.Text>}
                </Space>
              ),
            },
            { title: '说明', dataIndex: 'description', render: (v: string) => v || '-' },
            { title: '仓库地址', dataIndex: 'repository_url', render: (v: string) => v || '-' },
            { title: 'ID', dataIndex: 'id', width: 140, render: (v: string) => <Typography.Text code>{v}</Typography.Text> },
            {
              title: '操作',
              width: 220,
              render: (_, row) => (
                <Space>
                  <Button size="small" onClick={() => setProjectId(row.id)} disabled={row.id === projectId}>
                    设为当前
                  </Button>
                  <Button
                    size="small"
                    onClick={() => {
                      setEditing(row)
                      form.setFieldsValue(row)
                      setOpen(true)
                    }}
                  >
                    编辑
                  </Button>
                  <Popconfirm
                    title="删除项目会级联删除其下环境、用例、流程、计划，确认？"
                    onConfirm={async () => {
                      await client.delete(`/api/projects/${row.id}`)
                      message.success('已删除')
                      await reload()
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

      <Modal
        open={open}
        title={editing ? '编辑项目' : '新建项目'}
        onCancel={() => setOpen(false)}
        onOk={submit}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="项目名称" rules={[{ required: true, message: '请输入项目名称' }]}>
            <Input placeholder="例如：Demo 商城" />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input.TextArea rows={2} placeholder="这个项目测的是什么" />
          </Form.Item>
          <Form.Item name="repository_url" label="仓库地址">
            <Input placeholder="可选，用于关联 GitLab" />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
