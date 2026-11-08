import axios from 'axios'
import { GENERATION_HEADER, setGenerationToken } from './generation'

export const api = axios.create({ baseURL: '/api' })

// Every API response carries the erasure generation token (ADR 0015); keep the latest one.
// (Optional chaining: unit tests replace axios.create with a bare mock.)
api.interceptors?.response?.use(
  response => {
    const token = response.headers?.[GENERATION_HEADER]
    if (typeof token === 'string' && token) setGenerationToken(token)
    return response
  },
  error => {
    const token = error?.response?.headers?.[GENERATION_HEADER]
    if (typeof token === 'string' && token) setGenerationToken(token)
    return Promise.reject(error)
  },
)
