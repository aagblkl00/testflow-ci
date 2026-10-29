import { useCallback, useEffect, useState } from 'react'
import { client } from '../api/client'
import type { Project } from '../types'

const KEY = 'testflow_project'

/** 全局项目作用域：多数页面都挂在某一个项目下，统一在这里维护选中项。 */
export function useProjectScope() {
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectIdState] = useState<string>(() => localStorage.getItem(KEY) ?? '')
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const { data } = await client.get<Project[]>('/api/projects')
      setProjects(data)
      const current = localStorage.getItem(KEY)
      if ((!current || !data.some((item) => item.id === current)) && data.length > 0) {
        setProjectIdState(data[0].id)
        localStorage.setItem(KEY, data[0].id)
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const setProjectId = useCallback((value: string) => {
    setProjectIdState(value)
    localStorage.setItem(KEY, value)
  }, [])

  return { projects, projectId, setProjectId, loading, reload: load }
}
