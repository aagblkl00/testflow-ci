import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import 'antd/dist/reset.css'
import App from './App'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{ token: { borderRadius: 6 } }}
      // antd 默认会在「恰好两个汉字」的按钮文字中间插一个空格：
      // 「确认」变成「确 认」、「修改」变成「修 改」。中文后台里这个词距很别扭，
      // 而且会让「按按钮文字定位」的自动化脚本全部失效（我们的自测就因此挂过一次）。
      button={{ autoInsertSpace: false }}
    >
      <App />
    </ConfigProvider>
  </React.StrictMode>,
)
