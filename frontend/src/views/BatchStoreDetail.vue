<template>
  <section class="page" data-module="batch_store">
    <header class="page-head">
      <div>
        <h2>批次存储 · 详情与回执</h2>
        <p class="page-desc">
          存储按字段校验回执核查每行有效性；重验与来源更正并发时只确认一个可入库版本，历史版本都可查。
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" to="/batch_store">返回批次列表</RouterLink>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="load">
      <label class="filter-item">
        <span>批次编号</span>
        <input v-model="batchId" data-testid="detail-batch-id" />
      </label>
      <button class="btn primary" data-testid="detail-load" type="submit">读取详情</button>
      <button class="btn ghost" data-testid="detail-revalidate" type="button" @click="runRevalidate">
        发起重验
      </button>
      <button class="btn ghost" data-testid="detail-verify" type="button" @click="runVerify">
        独立校验摘要
      </button>
    </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
    <p v-if="notice" class="notice-text">{{ notice }}</p>

    <template v-if="detail">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">记录数</span>
          <strong class="stat-value">{{ detail.record_count }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">无效行</span>
          <strong class="stat-value" data-testid="detail-invalid">{{ detail.invalid_rows }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">已确认版本</span>
          <strong class="stat-value" data-testid="detail-active-version">
            {{ detail.active_receipt ? `V${detail.active_receipt.version}` : '未确认' }}
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">可用于验收</span>
          <strong class="stat-value" data-testid="detail-acceptance">
            {{ detail.acceptance_ready ? '是' : '否' }}
          </strong>
        </article>
      </div>

      <p class="status-legend">
        <span>清单摘要（v2）：{{ detail.manifest_checksum }}</span>
        <span>历史摘要：{{ detail.historical_checksum }}（{{ detail.historical_checksum_matches ? '可核对' : '不匹配' }}）</span>
      </p>

      <h3 class="section-title">存储清单</h3>
      <table class="data-table" data-testid="manifest-table">
        <thead>
          <tr>
            <th>行</th>
            <th>来源 URL</th>
            <th>抽取规则</th>
            <th>字段值（含类型）</th>
            <th>原始输入</th>
            <th>有效</th>
            <th>缺失字段</th>
            <th>失败原因</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in detail.manifest" :key="row.row_index">
            <tr
              :data-testid="`manifest-row-${row.row_index}`"
              :data-valid="row.valid"
              class="clickable-row"
              @click="toggleRow(row.row_index)"
            >
              <td>{{ row.row_index }}</td>
              <td>{{ row.source_url }}</td>
              <td>{{ row.extract_rule_id }}</td>
              <td>
                <span v-for="(cell, name) in row.fields" :key="name" class="field-chip">
                  {{ name }}={{ formatValue(cell.value) }}<em>[{{ cell.value_type }}]</em>
                </span>
              </td>
              <td>
                <span v-for="(value, name) in row.raw_fields" :key="name" class="field-chip raw-chip">
                  {{ name }}={{ formatValue(value) }}
                </span>
              </td>
              <td :class="row.valid ? 'ok-text' : 'error-text'">{{ row.valid ? '有效' : '无效' }}</td>
              <td>
                <span
                  v-for="name in row.missing_fields"
                  :key="name"
                  :data-testid="`missing-${row.row_index}-${name}`"
                >
                  {{ name }}（{{ row.field_sources[name] ?? '来源未知' }}）
                </span>
                <span v-if="!row.missing_fields.length">—</span>
              </td>
              <td class="error-text">{{ row.reasons.join('；') || '—' }}</td>
            </tr>
            <tr v-if="expandedRow === row.row_index" :data-testid="`manifest-expand-${row.row_index}`">
              <td :colspan="8" class="expand-cell">
                <table class="data-table inner-table">
                  <thead>
                    <tr><th>字段</th><th>保存值</th><th>类型</th><th>有效性</th><th>来源</th><th>说明</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="cell in row.field_receipts" :key="cell.field">
                      <td>{{ cell.field }}</td>
                      <td class="mono-cell">{{ formatValue(cell.value) }}</td>
                      <td>{{ cell.value_type }}</td>
                      <td :class="cell.status === 'valid' ? 'ok-text' : 'error-text'">{{ cell.status }}</td>
                      <td>{{ cell.source }}</td>
                      <td class="error-text">{{ cell.reason || '—' }}</td>
                    </tr>
                  </tbody>
                </table>
              </td>
            </tr>
          </template>
        </tbody>
      </table>

      <p class="hint-text">
        行内「原始输入」保留清洗前的值，空字段可据此回查所用抽取规则；点击任意行可展开逐字段回执。
      </p>

      <h3 class="section-title">来源更正</h3>
      <form class="filter-bar" @submit.prevent="runCorrection">
        <label class="filter-item">
          <span>字段</span>
          <input v-model="correction.field" data-testid="correction-field" />
        </label>
        <label class="filter-item">
          <span>新值（JSON）</span>
          <input v-model="correction.value" data-testid="correction-value" />
        </label>
        <label class="filter-item">
          <span>类型</span>
          <select v-model="correction.value_type" data-testid="correction-type">
            <option value="int">int</option>
            <option value="float">float</option>
            <option value="bool">bool</option>
            <option value="str">str</option>
          </select>
        </label>
        <label class="filter-item">
          <span>行号</span>
          <input v-model.number="correction.row_index" data-testid="correction-row" />
        </label>
        <label class="filter-item">
          <span>来源修订号</span>
          <input v-model="correction.source_revision" data-testid="correction-revision" />
        </label>
        <button class="btn" data-testid="correction-submit" type="submit">提交更正</button>
      </form>

      <h3 class="section-title">回执版本（同一字段冲突只确认一个）</h3>
      <table class="data-table" data-testid="receipt-table">
        <thead>
          <tr>
            <th>版本</th>
            <th>类型</th>
            <th>状态</th>
            <th>有效</th>
            <th>摘要</th>
            <th>来源修订</th>
            <th>判断范围</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="receipt in detail.receipts" :key="receipt.receipt_id" :data-testid="`receipt-${receipt.version}`">
            <td>V{{ receipt.version }}</td>
            <td>{{ receipt.kind }}</td>
            <td :class="receipt.status === '已确认' ? 'ok-text' : ''">{{ receipt.status }}</td>
            <td>{{ receipt.valid ? '是' : '否' }}</td>
            <td class="mono-cell">{{ receipt.checksum.slice(0, 24) }}…</td>
            <td>{{ receipt.source_revision || '—' }}</td>
            <td class="scope-cell">{{ receipt.judgement_scope.join(' / ') }}</td>
            <td class="row-actions">
              <button
                class="link"
                type="button"
                :data-testid="`confirm-${receipt.version}`"
                @click="runConfirm(receipt)"
              >
                确认此版本
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <h3 class="section-title">更正记录</h3>
      <table class="data-table">
        <thead>
          <tr><th>更正编号</th><th>版本</th><th>字段</th><th>旧值</th><th>新值</th><th>来源修订</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in detail.corrections" :key="item.correction_id">
            <td>{{ item.correction_id }}</td>
            <td>V{{ item.version }}</td>
            <td>{{ item.field }}</td>
            <td>{{ formatValue(item.old_value) }}</td>
            <td>{{ formatValue(item.new_value) }}</td>
            <td>{{ item.source_revision || '—' }}</td>
            <td>{{ item.created_at }}</td>
          </tr>
          <tr v-if="!detail.corrections.length">
            <td colspan="7" class="empty-state">暂无来源更正</td>
          </tr>
        </tbody>
      </table>

      <div v-if="lastConfirm" class="confirm-panel" :data-ok="lastConfirm.ok" data-testid="confirm-result">
        <strong>{{ lastConfirm.ok ? '确认成功' : '确认冲突' }}</strong>
        <span>{{ lastConfirm.message }}</span>
        <span v-if="lastConfirm.receipt">
          判断范围：{{ lastConfirm.receipt.judgement_scope.join(' / ') }}
        </span>
        <span v-if="lastConfirm.receipt">摘要：{{ lastConfirm.receipt.checksum }}</span>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  confirmReceipt,
  fetchBatchDetail,
  revalidateBatch,
  submitCorrection,
  verifyReceipt,
} from '@/api/backend-service'
import type { BatchDetail, ConfirmResult, Receipt } from '@/api/backend-service'

const props = defineProps<{ batchIdParam?: string }>()
const batchId = ref(props.batchIdParam ?? 'BATC-20261101-OLD')
const detail = ref<BatchDetail | null>(null)
const lastConfirm = ref<ConfirmResult | null>(null)
const expandedRow = ref<number | null>(null)
const errorMessage = ref('')
const notice = ref('')
const correction = ref({
  field: '金额',
  value: '0',
  value_type: 'int',
  row_index: 2,
  source_revision: 'rev-2026-11-20',
})

const hasCandidates = computed(
  () => (detail.value?.receipts ?? []).some((item) => item.status !== '已确认'),
)

function formatValue(value: unknown): string {
  if (value === '') return '""'
  if (value === null || value === undefined) return 'null'
  if (typeof value === 'boolean') return String(value)
  return String(value)
}

function toggleRow(rowIndex: number) {
  expandedRow.value = expandedRow.value === rowIndex ? null : rowIndex
}

async function load() {
  errorMessage.value = ''
  notice.value = ''
  try {
    detail.value = await fetchBatchDetail(batchId.value)
  } catch (error) {
    detail.value = null
    errorMessage.value = error instanceof Error ? error.message : '批次详情读取失败'
  }
}

async function runRevalidate() {
  errorMessage.value = ''
  try {
    const receipt = await revalidateBatch(batchId.value)
    notice.value = `已生成候选回执 ${receipt.receipt_id}（${receipt.status}）`
    await load()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重验失败'
  }
}

function parseCorrectionValue(): unknown {
  const text = correction.value.value.trim()
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

async function runCorrection() {
  errorMessage.value = ''
  try {
    const receipt = await submitCorrection(batchId.value, {
      field: correction.value.field,
      value: parseCorrectionValue(),
      value_type: correction.value.value_type,
      row_index: correction.value.row_index || null,
      source_revision: correction.value.source_revision,
      note: `来源修订 ${correction.value.source_revision} 更正 ${correction.value.field}`,
    })
    notice.value = `来源更正已生成候选回执 ${receipt.receipt_id}（${receipt.status}）`
    await load()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '来源更正失败'
  }
}

async function runConfirm(receipt: Receipt) {
  errorMessage.value = ''
  try {
    lastConfirm.value = await confirmReceipt(batchId.value, receipt.version, {
      idempotency_key: `confirm-${batchId.value}-V${receipt.version}`,
      expected_base_version: detail.value?.active_receipt?.version
        ?? receipt.base_version,
    })
    notice.value = lastConfirm.value.message
    await load()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '确认失败'
  }
}

async function runVerify() {
  errorMessage.value = ''
  try {
    const result = await verifyReceipt(batchId.value)
    notice.value = `${result.message}（重算摘要 ${result.checksum.slice(0, 24)}…）`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '独立校验失败'
  }
}

onMounted(load)
</script>

<style scoped>
.section-title {
  font-size: 15px;
  margin: 18px 0 8px;
}
.field-chip {
  display: inline-block;
  margin-right: 8px;
  white-space: nowrap;
}
.field-chip em {
  color: var(--muted);
  font-style: normal;
  font-size: 11px;
}
.mono-cell,
.scope-cell {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
}
.ok-text {
  color: #067647;
}
.notice-text {
  color: #175cd3;
}
.hint-text {
  color: var(--muted);
  font-size: 12px;
  margin-top: 6px;
}
.clickable-row {
  cursor: pointer;
}
.raw-chip {
  color: var(--muted);
}
.expand-cell {
  background: #fafbfc;
}
.inner-table th,
.inner-table td {
  font-size: 12px;
}
.confirm-panel {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 12px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #fff;
}
.confirm-panel[data-ok='false'] {
  border-color: #fda29b;
  background: #fffbfa;
}
</style>
