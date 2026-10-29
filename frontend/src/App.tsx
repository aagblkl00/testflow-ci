import { useEffect, useState } from 'react'
import { HashRouter, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { Layout, Menu, Space, Tag, Typography, Button } from 'antd'
import {
  AppstoreOutlined,
  CloudServerOutlined,
  DashboardOutlined,
  ExperimentOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  PlayCircleOutlined,
  ProjectOutlined,
  ScheduleOutlined,
} from '@ant-design/icons'
import { clearSession, getToken, USER_KEY } from './api/client'
import type { User } from './types'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Projects from './pages/Projects'
import Environments from './pages/Environments'
import Cases from './pages/Cases'
import Flows from './pages/Flows'
import Suites from './pages/Suites'
import Plans from './pages/Plans'
import Runs from './pages/Runs'
import RunDetail from './pages/RunDetail'
import Failures from './pages/Failures'
import Placeholder from './pages/Placeholder'

const { Sider, Header, Content } = Layout

const MENU = [
  { key: '/dashboard', icon: <DashboardOutlined />, label: '工作台' },
  { key: '/projects', icon: <ProjectOutlined />, label: '项目' },
  { key: '/environments', icon: <CloudServerOutlined />, label: '环境' },
  { key: '/cases', icon: <FileTextOutlined />, label: '测试用例' },
  { key: '/flows', icon: <AppstoreOutlined />, label: '自动化流程' },
  { key: '/suites', icon: <FolderOpenOutlined />, label: '测试套件' },
  { key: '/plans', icon: <ScheduleOutlined />, label: '回归计划' },
  { key: '/runs', icon: <PlayCircleOutlined />, label: '执行中心' },
  { key: '/failures', icon: <ExperimentOutlined />, label: '失败中心' },
]

function Shell() {
  const navigate = useNavigate()
  const location = useLocation()
  const [user, setUser] = useState<User | null>(null)

  useEffect(() => {
    const raw = localStorage.getItem(USER_KEY)
    if (raw) {
      try {
        setUser(JSON.parse(raw))
      } catch {
        setUser(null)
      }
    }
  }, [])

  if (!getToken()) return <Navigate to="/login" replace />

  const selected = MENU.map((item) => item.key)
    .filter((key) => location.pathname === key || location.pathname.startsWith(`${key}/`))
    .sort((a, b) => b.length - a.length)[0]

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider theme="light" width={208} style={{ borderRight: '1px solid #f0f0f0' }}>
        <div style={{ padding: '18px 20px 14px' }}>
          <Typography.Text strong style={{ fontSize: 16 }}>
            TestFlow CI
          </Typography.Text>
          <div style={{ fontSize: 12, color: '#8c8c8c' }}>Web 自动化持续回归</div>
        </div>
        <Menu
          mode="inline"
          selectedKeys={selected ? [selected] : []}
          items={MENU}
          onClick={({ key }) => navigate(key)}
          style={{ borderInlineEnd: 'none' }}
        />
      </Sider>
      <Layout>
        <Header
          style={{
            background: '#fff',
            borderBottom: '1px solid #f0f0f0',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            paddingInline: 20,
            height: 56,
          }}
        >
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            M3 失败中心 · 确定性指纹聚合 / 规则分类 / Flaky 统计 · 执行模式 inline
          </Typography.Text>
          <Space>
            {user && (
              <>
                <Typography.Text>{user.display_name || user.username}</Typography.Text>
                <Tag color={user.role === 'ADMIN' ? 'blue' : user.role === 'TESTER' ? 'green' : 'default'}>
                  {user.role}
                </Tag>
              </>
            )}
            <Button
              size="small"
              onClick={() => {
                clearSession()
                navigate('/login')
              }}
            >
              退出
            </Button>
          </Space>
        </Header>
        <Content style={{ padding: 20, background: '#f5f7fa' }}>
          <Routes>
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/projects" element={<Projects />} />
            <Route path="/environments" element={<Environments />} />
            <Route path="/cases" element={<Cases />} />
            <Route path="/flows" element={<Flows />} />
            <Route path="/suites" element={<Suites />} />
            <Route path="/plans" element={<Plans />} />
            <Route path="/runs" element={<Runs />} />
            <Route path="/runs/:runId" element={<RunDetail />} />
            <Route path="/failures" element={<Failures />} />
            <Route path="/ai" element={<Placeholder title="AI 分析" milestone="M5" />} />
            <Route path="/notifications" element={<Placeholder title="企业微信通知" milestone="M5" />} />
            <Route path="/*" element={<Navigate to="/dashboard" replace />} />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  )
}

export default function App() {
  return (
    <HashRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/*" element={<Shell />} />
      </Routes>
    </HashRouter>
  )
}
