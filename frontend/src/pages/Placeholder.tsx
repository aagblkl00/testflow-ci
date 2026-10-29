import { Card, Empty, Typography } from 'antd'

export default function Placeholder({ title, milestone }: { title: string; milestone: string }) {
  return (
    <Card>
      <Typography.Title level={5} style={{ marginTop: 0 }}>
        {title}
      </Typography.Title>
      <Empty
        description={
          <span>
            该页面已预留路由，计划在 <Typography.Text strong>{milestone}</Typography.Text> 实现。
            <br />
            M1 的目标是先把「真实执行闭环」跑通，UI 不做提前开发。
          </span>
        }
      />
    </Card>
  )
}
