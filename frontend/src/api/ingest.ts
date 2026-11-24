import { request } from './client'

/** 入库确认明细（与后端 /api/ingest/receipts 结构对齐）。 */
export type ReceiptView = {
  receipt_id: number
  批次标识: string
  来源标识: string
  提交标识: string
  调度任务: string | null
  存储批次: number
  原始行数: number
  有效行数: number
  内容摘要: string
  确认状态: '待确认' | '已确认'
  送达次数: number
  首次接收时间: string
  最近接收时间: string
  确认时间: string | null
  确认结果: { matched: boolean; 确认时间: string; 核对行数: number; 内容摘要: string } | null
  送达记录: { 时间: string; 提交标识: string; 原始行数: number; 内容摘要: string }[]
  落库结果行?: Record<string, unknown>[]
}

export type TrackingBatch = {
  track_id: number
  批次标识: string
  来源标识: string
  调度任务: string | null
  批次状态: string
  入库状态: '待确认' | '已确认'
  采集总数: string
  失败行数: string
  已确认提交: number
  待确认提交: number
  送达次数: number
  首次提交: { 提交标识: string; 首次接收时间: string; 内容摘要: string; 原始行数: number } | null
  开始时间: string
  最近接收时间: string | null
  结束时间: string | null
  确认明细: {
    receipt_id: number
    提交标识: string
    来源标识: string
    原始行数: number
    有效行数: number
    内容摘要: string
    确认状态: '待确认' | '已确认'
    送达次数: number
    首次接收时间: string
    最近接收时间: string
    确认时间: string | null
    存储批次: number
  }[]
  pending_receipt_ids: number[]
  status: string
  pending: boolean
  abnormal: boolean
}

export type DeliveryResponse = {
  ok: boolean
  duplicate: boolean
  message: string
  receipt: ReceiptView
  store_batch: Record<string, unknown>
  tracking: TrackingBatch | null
}

export type ConfirmResponse = {
  ok: boolean
  message: string
  receipt: ReceiptView
  tracking: TrackingBatch | null
}

async function asJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = ''
    try {
      detail = (await response.json())?.detail ?? ''
    } catch {
      detail = ''
    }
    throw new Error(detail || `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

export async function deliverResult(payload: {
  batch_id: string
  source_key: string
  commit_id: string
  rows: Record<string, unknown>[]
  valid_count?: number
}): Promise<DeliveryResponse> {
  const response = await request('/api/ingest/deliveries', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return asJson<DeliveryResponse>(response)
}

export async function confirmReceipt(receiptId: number): Promise<ConfirmResponse> {
  const response = await request(`/api/ingest/receipts/${receiptId}/confirm`, { method: 'POST' })
  return asJson<ConfirmResponse>(response)
}

export async function loadTrackingOverview(): Promise<{
  批次总数: number
  待确认批次: number
  已确认批次: number
  实际落库行数: number
  items: TrackingBatch[]
}> {
  const response = await request('/api/ingest/tracking')
  return asJson(response)
}

export async function lookupTracking(
  batchId: string,
  sourceKey?: string,
): Promise<{ items: TrackingBatch[]; total: number }> {
  const params = new URLSearchParams({ batch_id: batchId })
  if (sourceKey) params.set('source_key', sourceKey)
  const response = await request(`/api/ingest/tracking/lookup?${params.toString()}`)
  return asJson(response)
}

export async function loadReceipt(receiptId: number): Promise<ReceiptView> {
  const response = await request(`/api/ingest/receipts/${receiptId}`)
  return asJson<ReceiptView>(response)
}
