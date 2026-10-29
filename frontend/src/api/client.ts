import axios from 'axios'
import { message } from 'antd'

export const TOKEN_KEY = 'testflow_token'
export const USER_KEY = 'testflow_user'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export function setSession(token: string, user: unknown): void {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function clearSession(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

export const client = axios.create({ baseURL: '', timeout: 600000 })

client.interceptors.request.use((config) => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

client.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error?.response?.status
    const detail = error?.response?.data?.detail
    if (status === 401) {
      clearSession()
      if (!window.location.hash.includes('/login')) window.location.hash = '#/login'
    }
    if (detail && status !== 401) message.error(String(detail))
    else if (!error?.response) message.error('无法连接 TestFlow 后端，请确认服务已启动')
    return Promise.reject(error)
  },
)
