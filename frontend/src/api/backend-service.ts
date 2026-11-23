import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---------------------------------------------------------------------------
// 字段清洗复核 & 批次存储回执
// ---------------------------------------------------------------------------

export type FieldReceipt = {
  field: string
  value: string | number | boolean | null
  value_type: string
  status: string
  source: string
  reason: string
}

export type ReviewRow = {
  row_index: number
  source_url: string
  extract_rule_id: string
  valid: boolean
  fields: Record<string, { value: string | number | boolean | null; value_type: string }>
  raw_fields: Record<string, string | number | boolean | null>
  field_sources: Record<string, string>
  missing_fields: string[]
  reasons: string[]
  field_receipts: FieldReceipt[]
  digest: string
  input: Record<string, string | number | boolean | null>
}

export type ReviewResult = {
  review_id: string
  extract_rule_id: string
  source_url: string
  logic: string
  rule_ids: string[]
  validity_rule: string
  rows: ReviewRow[]
  valid_rows: number
  missing_row_count: number
  storable: boolean
}

export type ReceiptRow = {
  row_index: number
  source_url: string
  extract_rule_id: string
  valid: boolean
  fields: Record<string, { value: string | number | boolean | null; value_type: string }>
  raw_fields: Record<string, string | number | boolean | null>
  field_sources: Record<string, string>
  missing_fields: string[]
  reasons: string[]
  field_receipts: FieldReceipt[]
  digest: string
}

export type Receipt = {
  receipt_id: string
  batch_id: string
  version: number
  kind: string
  status: string
  valid: boolean
  checksum: string
  legacy_checksum: string
  stored_legacy_checksum: string
  legacy_checksum_matches: boolean
  judgement_scope: string[]
  source_revision: string
  idempotency_key: string
  base_version: number
  corrected_fields: string[]
  created_at: string
  confirmed_at: string
  confirmed_by: string
  superseded_by: number | null
  note: string
  row_count: number
  valid_rows: number
  missing_row_count: number
  rows: ReceiptRow[]
}

export type Correction = {
  correction_id: string
  batch_id: string
  version: number
  field: string
  old_value: unknown
  new_value: unknown
  value_type: string
  source_revision: string
  source_url: string
  status: string
  note: string
  created_at: string
}

export type BatchDetail = {
  batch: EntryRow
  stored_checksum: string
  historical_checksum: string
  historical_checksum_matches: boolean
  storage_path: string
  manifest: ReceiptRow[]
  manifest_checksum: string
  legacy_checksum: string
  legacy_checksum_matches: boolean
  record_count: number
  invalid_rows: number
  active_receipt: Receipt | null
  latest_receipt: Receipt | null
  effective_receipt: Receipt | null
  source_revision: string
  acceptance_ready: boolean
  acceptance_receipt_id: string
  acceptance_checksum: string
  receipts: Receipt[]
  corrections: Correction[]
}

export type ConfirmResult = {
  ok: boolean
  message: string
  batch_id: string
  version: number
  active_version: number
  receipt: Receipt | null
  acceptance_ready: boolean
}

export async function reviewFieldInputs(
  inputs: Record<string, unknown>[],
  extractRuleId = 'CONT-REVIEW-001',
): Promise<ReviewResult> {
  const response = await request('/api/field_clean/review', {
    method: 'POST',
    body: JSON.stringify({ inputs, extract_rule_id: extractRuleId }),
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '字段清洗复核失败')
  return payload
}

export async function fetchBatchDetail(batchId: string): Promise<BatchDetail> {
  const response = await request(`/api/batch_store/${encodeURIComponent(batchId)}/detail`)
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '批次详情读取失败')
  return payload
}

export async function fetchReceipt(batchId: string, version: number): Promise<Receipt> {
  const response = await request(
    `/api/batch_store/${encodeURIComponent(batchId)}/receipts/${version}`,
  )
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '回执读取失败')
  return payload
}

export async function revalidateBatch(batchId: string, note = ''): Promise<Receipt> {
  const response = await request(`/api/batch_store/${encodeURIComponent(batchId)}/revalidate`, {
    method: 'POST',
    body: JSON.stringify({ note }),
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '重验失败')
  return payload
}

export async function submitCorrection(
  batchId: string,
  body: {
    field: string
    value: unknown
    value_type?: string
    source_revision?: string
    source_url?: string
    row_index?: number | null
    note?: string
  },
): Promise<Receipt> {
  const response = await request(`/api/batch_store/${encodeURIComponent(batchId)}/corrections`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '来源更正失败')
  return payload
}

export async function confirmReceipt(
  batchId: string,
  version: number,
  extra: { idempotency_key?: string; expected_base_version?: number | null } = {},
): Promise<ConfirmResult> {
  const response = await request(
    `/api/batch_store/${encodeURIComponent(batchId)}/receipts/${version}/confirm`,
    {
      method: 'POST',
      body: JSON.stringify({
        idempotency_key: extra.idempotency_key ?? '',
        expected_base_version: extra.expected_base_version ?? null,
      }),
    },
  )
  return await response.json()
}

export async function verifyReceipt(
  batchId: string,
  version?: number,
): Promise<{ ok: boolean; checksum: string; message: string; manifest_row_count: number }> {
  const response = await request(`/api/batch_store/${encodeURIComponent(batchId)}/verify`, {
    method: 'POST',
    body: JSON.stringify({ version: version ?? null }),
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '独立校验失败')
  return payload
}
