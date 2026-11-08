import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { getAutostart, updateAutostart } from '../../api/client'
import { Switch } from '../Switch'
import { useToast } from '../toastContext'
import { AUTOSTART_QUERY_KEY } from './queryKeys'

/** Start with Windows. Shown only by the installed app on Windows, where it can work. */
export function AutostartSwitch() {
  const { t } = useTranslation()
  const toast = useToast()
  const queryClient = useQueryClient()
  const { data } = useQuery({ queryKey: AUTOSTART_QUERY_KEY, queryFn: getAutostart, retry: false })
  const save = useMutation({
    mutationFn: updateAutostart,
    onSuccess: saved => queryClient.setQueryData(AUTOSTART_QUERY_KEY, saved),
    onError: () => toast.show({ message: t('settings.background.autostartFailed'), tone: 'danger' }),
  })
  if (!data?.supported) return null
  return (
    <Switch
      checked={data.enabled}
      disabled={save.isPending}
      onChange={enabled => save.mutate(enabled)}
      label={t('settings.background.autostart')}
      description={t('settings.background.autostartHelp')}
    />
  )
}
