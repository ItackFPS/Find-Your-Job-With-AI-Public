import { useQuery } from '@tanstack/react-query'
import { getCollectionStatus } from '../api/client'

export const COLLECTION_STATUS_QUERY_KEY = ['collection-status'] as const

/** Whether collection is paused (owner decision D7): no source is contacted until it is checked and
 *  turned back on. Undefined while loading or when the status cannot be read. */
export function useCollectionFrozen(): boolean | undefined {
  const q = useQuery({ queryKey: COLLECTION_STATUS_QUERY_KEY, queryFn: getCollectionStatus, retry: false })
  return q.data?.frozen
}
