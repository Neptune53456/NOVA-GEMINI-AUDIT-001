import { useEffect, useState } from 'react'
import { getBusinessData, type GodEyesEndpoint } from '../../api/novaApi'
import type { DataRow } from './businessData'

export type BusinessResource = { data?: DataRow; error?: string }
export function useBusinessResource(endpoint: GodEyesEndpoint) {
  const [result, setResult] = useState<BusinessResource & { endpoint?: string }>({})
  useEffect(() => {
    const controller = new AbortController()
    getBusinessData(endpoint, controller.signal).then(data => {
      if (!controller.signal.aborted) setResult({ endpoint, data })
    }).catch(error => {
      if (!controller.signal.aborted) setResult({ endpoint, error: error instanceof Error ? error.message : 'The backend could not be reached.' })
    })
    return () => controller.abort()
  }, [endpoint])
  return result.endpoint === endpoint ? result : {}
}
