import axios, { AxiosError } from 'axios'

export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '/api/v1'

export const http = axios.create({
  baseURL: API_BASE_URL,
  timeout: 180_000,
})

/** FastAPI 的 detail 可能是字符串，也可能是 422 校验错误数组。 */
export function apiErrorMessage(error: unknown): string {
  if (error instanceof AxiosError) {
    const detail = error.response?.data?.detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      return detail
        .map((item) => `${(item.loc ?? []).slice(1).join('.')}: ${item.msg}`)
        .join('; ')
    }
    return error.message
  }
  return error instanceof Error ? error.message : '未知错误'
}
