<template>
  <section class="page" data-module="batch-tracking-console">
    <header class="page-head">
      <div>
        <h2>批次追踪 · 入库确认台</h2>
        <p class="page-desc">
          追踪数据实时读取入库确认明细：核对每个来源批次的实际落库行数、首次收到的提交版本，
          确认中断可在此断点续做；同一提交重复送达不会重复入库。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="refresh-tracking" type="button" @click="reload">刷新追踪</button>
        <button class="btn primary" data-testid="open-deliver" type="button" @click="openDeliver">
          模拟来源送达
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">追踪批次总数</span>
        <strong class="stat-value">{{ overview.批次总数 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待确认批次</span>
        <strong class="stat-value" data-testid="stat-pending">{{ overview.待确认批次 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已确认批次</span>
        <strong class="stat-value" data-testid="stat-confirmed">{{ overview.已确认批次 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">实际落库行数</span>
        <strong class="stat-value" data-testid="stat-rows">{{ overview.实际落库行数 }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="search">
      <label class="filter-item">
        <span>批次标识</span>
        <input v-model="query.batchId" data-testid="filter-batch" placeholder="按批次标识复查，如 LBL-DUTY" />
      </label>
      <label class="filter-item">
        <span>来源标识</span>
        <input v-model="query.sourceKey" data-testid="filter-source" placeholder="可选，同标签按来源区分" />
      </label>
      <button class="btn" data-testid="query-tracking" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetQuery">重置条件</button>
    </form>

    <p v-if="message" class="page-foot" :class="messageKind">{{ message }}</p>

    <table class="data-table" data-testid="tracking-table">
      <thead>
        <tr>
          <th>批次标识</th>
          <th>来源标识</th>
          <th>入库状态</th>
          <th>实际落库行数</th>
          <th>待确认/已确认提交</th>
          <th>累计送达次数</th>
          <th>首次收到版本</th>
          <th>最近接收</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="batch in batches"
          :key="`${batch.批次标识}-${batch.来源标识}`"
          :data-testid="`track-${batch.track_id}`"
          :data-status="batch.入库状态"
        >
          <td>{{ batch.批次标识 }}</td>
          <td>{{ batch.来源标识 }}</td>
          <td>
            <span :class="['status-pill', batch.入库状态 === '已确认' ? 'ok' : 'pending']">
              {{ batch.入库状态 }}
            </span>
          </td>
          <td data-testid="stored-rows">{{ batch.采集总数 }}</td>
          <td>{{ batch.待确认提交 }} / {{ batch.已确认提交 }}</td>
          <td>{{ batch.送达次数 }}</td>
          <td>
            <template v-if="batch.首次提交">
              {{ batch.首次提交.提交标识 }}（{{ batch.首次提交.原始行数 }} 行）
            </template>
            <template v-else>—</template>
          </td>
          <td>{{ batch.最近接收时间 ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-if="batch.pending_receipt_ids.length"
              class="link"
              type="button"
              :data-testid="`confirm-${batch.track_id}`"
              @click="confirmPending(batch)"
            >
              补全确认
            </button>
            <button class="link" type="button" :data-testid="`detail-${batch.track_id}`" @click="openDetail(batch)">
              入库明细
            </button>
          </td>
        </tr>
        <tr v-if="!batches.length">
          <td colspan="9" class="empty-state">暂无追踪批次，可先模拟来源送达</td>
        </tr>
      </tbody>
    </table>

    <!-- 模拟来源送达 -->
    <div v-if="showDeliver" class="modal-backdrop" data-testid="deliver-modal">
      <form class="modal" @submit.prevent="submitDeliver">
        <h3>模拟来源送达结果</h3>
        <label class="create-field">
          <span>批次标识（同标签不同来源各自独立）</span>
          <input v-model="deliverForm.batch_id" data-testid="deliver-batch" required />
        </label>
        <label class="create-field">
          <span>来源标识</span>
          <input v-model="deliverForm.source_key" data-testid="deliver-source" required />
        </label>
        <label class="create-field">
          <span>提交标识（同一提交重送走幂等，不重复入库）</span>
          <input v-model="deliverForm.commit_id" data-testid="deliver-commit" required />
        </label>
        <label class="create-field">
          <span>结果行数（按行数生成原始结果行）</span>
          <input v-model.number="deliverForm.row_count" data-testid="deliver-rows" type="number" min="1" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showDeliver = false">取消</button>
          <button class="btn primary" data-testid="submit-deliver" type="submit" :disabled="submitting">
            {{ submitting ? '送达中…' : '送达结果' }}
          </button>
        </div>
      </form>
    </div>

    <!-- 入库确认明细 -->
    <div v-if="detailReceipt" class="modal-backdrop" data-testid="receipt-modal">
      <div class="modal modal-wide">
        <h3>入库确认明细 #{{ detailReceipt.receipt_id }}</h3>
        <dl class="detail-grid">
          <dt>批次标识</dt><dd>{{ detailReceipt.批次标识 }}</dd>
          <dt>来源标识</dt><dd>{{ detailReceipt.来源标识 }}</dd>
          <dt>提交标识</dt><dd>{{ detailReceipt.提交标识 }}</dd>
          <dt>确认状态</dt><dd>{{ detailReceipt.确认状态 }}</dd>
          <dt>原始/有效行数</dt><dd>{{ detailReceipt.原始行数 }} / {{ detailReceipt.有效行数 }}</dd>
          <dt>累计送达次数</dt><dd>{{ detailReceipt.送达次数 }}</dd>
          <dt>内容摘要</dt><dd class="digest">{{ detailReceipt.内容摘要 }}</dd>
          <dt>首次接收</dt><dd>{{ detailReceipt.首次接收时间 }}</dd>
          <dt>确认时间</dt><dd>{{ detailReceipt.确认时间 ?? '—' }}</dd>
          <dt>存储批次</dt><dd>#{{ detailReceipt.存储批次 }}</dd>
        </dl>

        <h4>每次实际收到的送达凭据</h4>
        <table class="data-table inner-table">
          <thead>
            <tr><th>送达时间</th><th>提交标识</th><th>原始行数</th><th>内容摘要</th></tr>
          </thead>
          <tbody>
            <tr v-for="(item, idx) in detailReceipt.送达记录" :key="idx">
              <td>{{ item.时间 }}</td>
              <td>{{ item.提交标识 }}</td>
              <td>{{ item.原始行数 }}</td>
              <td class="digest">{{ item.内容摘要 }}</td>
            </tr>
          </tbody>
        </table>

        <h4>实际落库结果行（首次收到的原始结果，重送不覆盖）</h4>
        <pre class="rows-preview">{{ JSON.stringify(detailReceipt.落库结果行 ?? [], null, 2) }}</pre>

        <div class="page-actions">
          <button class="btn" type="button" @click="detailReceipt = null">关闭</button>
          <button
            v-if="detailReceipt.确认状态 === '待确认'"
            class="btn primary"
            data-testid="confirm-in-detail"
            type="button"
            :disabled="submitting"
            @click="confirmOne(detailReceipt.receipt_id)"
          >
            确认入库
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import {
  confirmReceipt,
  deliverResult,
  loadReceipt,
  loadTrackingOverview,
  lookupTracking,
} from '@/api/ingest'
import type { ReceiptView, TrackingBatch } from '@/api/ingest'

type Overview = Awaited<ReturnType<typeof loadTrackingOverview>>

const overview = ref<Overview>({
  批次总数: 0,
  待确认批次: 0,
  已确认批次: 0,
  实际落库行数: 0,
  items: [],
})
const batches = ref<TrackingBatch[]>([])
const query = reactive({ batchId: '', sourceKey: '' })
const message = ref('')
const messageKind = ref('')
const showDeliver = ref(false)
const submitting = ref(false)
const detailReceipt = ref<ReceiptView | null>(null)
const deliverForm = reactive({
  batch_id: 'LBL-DUTY',
  source_key: 'src-a',
  commit_id: 'cm-duty-a',
  row_count: 9,
})

function flash(text: string, ok = true) {
  message.value = text
  messageKind.value = ok ? 'ok-text' : 'error-text'
}

async function reload() {
  try {
    overview.value = await loadTrackingOverview()
    if (query.batchId.trim()) {
      const result = await lookupTracking(query.batchId.trim(), query.sourceKey.trim() || undefined)
      batches.value = result.items
    } else {
      batches.value = overview.value.items
    }
  } catch (error) {
    flash(error instanceof Error ? error.message : '批次追踪读取失败', false)
  }
}

async function search() {
  message.value = ''
  if (!query.batchId.trim()) {
    await reload()
    return
  }
  try {
    const result = await lookupTracking(query.batchId.trim(), query.sourceKey.trim() || undefined)
    batches.value = result.items
    if (!result.total) flash('未查到对应追踪批次', false)
  } catch (error) {
    flash(error instanceof Error ? error.message : '追踪复查失败', false)
  }
}

function resetQuery() {
  query.batchId = ''
  query.sourceKey = ''
  void reload()
}

function openDeliver() {
  message.value = ''
  showDeliver.value = true
}

async function submitDeliver() {
  submitting.value = true
  try {
    const rows = Array.from({ length: Number(deliverForm.row_count) }, (_, index) => ({
      row_no: index + 1,
      batch_label: deliverForm.batch_id,
      source: deliverForm.source_key,
      payload: `${deliverForm.source_key}-row-${index + 1}`,
    }))
    const result = await deliverResult({
      batch_id: deliverForm.batch_id,
      source_key: deliverForm.source_key,
      commit_id: deliverForm.commit_id,
      rows,
    })
    flash(
      result.duplicate
        ? `重复送达：保留首次落库的 ${result.receipt.原始行数} 行，累计送达 ${result.receipt.送达次数} 次，未重复入库`
        : `结果已落库 ${result.receipt.原始行数} 行，确认明细 #${result.receipt.receipt_id} 等待确认`,
      true,
    )
    showDeliver.value = false
    await reload()
  } catch (error) {
    flash(error instanceof Error ? error.message : '送达失败', false)
  } finally {
    submitting.value = false
  }
}

async function confirmPending(batch: TrackingBatch) {
  try {
    for (const receiptId of batch.pending_receipt_ids) {
      const result = await confirmReceipt(receiptId)
      flash(result.message, result.ok)
    }
    await reload()
  } catch (error) {
    flash(error instanceof Error ? error.message : '确认失败', false)
  }
}

async function confirmOne(receiptId: number) {
  submitting.value = true
  try {
    const result = await confirmReceipt(receiptId)
    flash(result.message, result.ok)
    detailReceipt.value = await loadReceipt(receiptId)
    await reload()
  } catch (error) {
    flash(error instanceof Error ? error.message : '确认失败', false)
  } finally {
    submitting.value = false
  }
}

async function openDetail(batch: TrackingBatch) {
  message.value = ''
  const first = batch.确认明细[0]
  if (!first) {
    flash('该追踪批次暂无入库确认明细', false)
    return
  }
  detailReceipt.value = await loadReceipt(first.receipt_id)
}

onMounted(reload)
</script>

<style scoped>
.status-pill {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
}
.status-pill.ok {
  background: #dcfce7;
  color: #15803d;
}
.status-pill.pending {
  background: #fef3c7;
  color: #b45309;
}
.modal-wide {
  width: min(820px, 100%);
}
.detail-grid {
  display: grid;
  grid-template-columns: 120px 1fr;
  gap: 6px 12px;
  margin: 8px 0 16px;
  font-size: 13px;
}
.detail-grid dt {
  color: var(--muted);
}
.detail-grid dd {
  margin: 0;
}
.digest {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  word-break: break-all;
}
.inner-table {
  margin-bottom: 16px;
}
.rows-preview {
  max-height: 220px;
  overflow: auto;
  background: #0f172a;
  color: #e2e8f0;
  border-radius: 8px;
  padding: 12px;
  font-size: 12px;
}
.ok-text {
  color: #15803d;
}
</style>
