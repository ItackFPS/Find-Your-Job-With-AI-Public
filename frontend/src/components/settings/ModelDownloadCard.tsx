import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { getEmbeddingModel, startEmbeddingModelDownload } from '../../api/client'
import { Button } from '../Button'
import { Card } from '../Card'
import { ErrorState } from '../ErrorState'
import { LoadingState } from '../LoadingState'
import { ProgressBar } from '../ProgressBar'
import { EMBEDDING_MODEL_QUERY_KEY } from './queryKeys'

const MB = 1024 * 1024

/**
 * The local matching model: its size, the download's progress, what went wrong and a retry.
 * Standalone, so the first-run wizard can show the same card. Searches work without the model:
 * results are then ranked by the rules alone.
 */
export function ModelDownloadCard() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: EMBEDDING_MODEL_QUERY_KEY,
    queryFn: getEmbeddingModel,
    refetchInterval: query => (query.state.data?.state === 'downloading' ? 1000 : false),
  })
  const start = useMutation({
    mutationFn: startEmbeddingModelDownload,
    onSuccess: status => queryClient.setQueryData(EMBEDDING_MODEL_QUERY_KEY, status),
  })
  const sizeMb = data ? Math.round(data.total_bytes / MB) : 0

  return (
    <Card title={t('settings.model.title')} subtitle={t('settings.model.subtitle')}>
      {isLoading && <LoadingState />}
      {isError && <ErrorState message={t('settings.loadFailed')} onRetry={() => void refetch()} />}
      {data && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          {data.state === 'ready' && <p style={{ margin: 0 }}>{t('settings.model.ready')}</p>}
          {data.state === 'downloading' && (
            <ProgressBar
              value={(data.downloaded_bytes / data.total_bytes) * 100}
              label={t('settings.model.downloading')}
              rightLabel={t('settings.model.progress', { done: Math.round(data.downloaded_bytes / MB), total: sizeMb })}
            />
          )}
          {data.state === 'missing' && <p style={{ margin: 0 }}>{t('settings.model.missing', { size: sizeMb })}</p>}
          {data.state === 'failed' && (
            <p role="alert" style={{ margin: 0, color: 'var(--color-error)' }}>
              {t(`settings.model.error.${data.error ?? 'failed'}`)}
            </p>
          )}
          {(data.state === 'missing' || data.state === 'failed') && (
            <div>
              <Button onClick={() => start.mutate()} disabled={start.isPending}>
                {t(data.state === 'failed' ? 'settings.model.retry' : 'settings.model.download')}
              </Button>
            </div>
          )}
          {data.state !== 'ready' && (
            <p style={{ margin: 0, fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>
              {t('settings.model.optional')}
            </p>
          )}
        </div>
      )}
    </Card>
  )
}
