import type { ReactNode } from 'react'
import { LoadingState } from './LoadingState'
import { EmptyState } from './EmptyState'

export interface DataTableColumn<T> {
  key: string
  header: string
  align?: 'left' | 'right' | 'center'
  width?: string
  render: (row: T) => ReactNode
}

export interface DataTableProps<T> {
  columns: DataTableColumn<T>[]
  rows: T[] | undefined
  rowKey: (row: T) => string
  loading?: boolean
  emptyMessage?: string
  onRowClick?: (row: T) => void
}

/**
 * Structural table foundation — columns/rows/cell rendering plus the
 * loading/empty/responsive-overflow states every table in this app needs.
 * Sorting and filtering are out of scope for this phase; columns just
 * render whatever `rows` they're given.
 */
export function DataTable<T>({ columns, rows, rowKey, loading = false, emptyMessage = 'No data.', onRowClick }: DataTableProps<T>) {
  if (loading) return <LoadingState compact />
  if (!rows || rows.length === 0) return <EmptyState message={emptyMessage} />

  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 'var(--font-size-sm)' }}>
        <thead>
          <tr style={{ borderBottom: '1px solid var(--color-border)' }}>
            {columns.map(col => (
              <th
                key={col.key}
                style={{
                  textAlign: col.align ?? 'left',
                  padding: '0.35rem 0.5rem',
                  fontWeight: 'var(--font-weight-semibold)',
                  color: 'var(--color-muted)',
                  width: col.width,
                  whiteSpace: 'nowrap',
                }}
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(row => {
            const key = rowKey(row)
            const clickable = !!onRowClick
            return (
              <tr
                key={key}
                onClick={clickable ? () => onRowClick!(row) : undefined}
                tabIndex={clickable ? 0 : undefined}
                onKeyDown={
                  clickable
                    ? e => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault()
                          onRowClick!(row)
                        }
                      }
                    : undefined
                }
                style={{
                  borderBottom: '1px solid var(--color-border)33',
                  cursor: clickable ? 'pointer' : undefined,
                }}
              >
                {columns.map(col => (
                  <td key={col.key} style={{ padding: '0.35rem 0.5rem', textAlign: col.align ?? 'left' }}>
                    {col.render(row)}
                  </td>
                ))}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
