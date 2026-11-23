<template>
  <section class="page" data-module="field_clean">
    <header class="page-head">
      <div>
        <h2>字段清洗 · 输入复核</h2>
        <p class="page-desc">
          按真实输入验证清洗规则：数字 0、布尔 false 都会按字段类型保存；只有空字符串 / 空值才算真实空项。
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" to="/field_clean">返回清洗规则</RouterLink>
      </div>
    </header>

    <p class="status-legend">{{ validityRule }}</p>

    <form class="filter-bar" @submit.prevent="runReview">
      <label v-for="(row, index) in inputs" :key="index" class="filter-item">
        <span>第 {{ index + 1 }} 行原始输入</span>
        <textarea
          v-model="inputTexts[index]"
          rows="3"
          cols="34"
          :data-testid="`review-input-${index}`"
          spellcheck="false"
        />
      </label>
      <div class="filter-item">
        <span>抽取规则</span>
        <input v-model="extractRuleId" data-testid="review-extract-rule" />
      </div>
      <button class="btn primary" data-testid="review-submit" type="submit" :disabled="loading">
        {{ loading ? '复核中…' : '复核输入' }}
      </button>
      <button class="btn ghost" data-testid="review-reset" type="button" @click="resetInputs">
        还原示例
      </button>
    </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <template v-if="result">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">复核行数</span>
          <strong class="stat-value">{{ result.rows.length }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">有效行</span>
          <strong class="stat-value">{{ result.valid_rows }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">含真实空项的行</span>
          <strong class="stat-value">{{ result.missing_row_count }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">可入库</span>
          <strong class="stat-value" :data-testid="'review-storable'">
            {{ result.storable ? '是' : '否' }}
          </strong>
        </article>
      </div>

      <table class="data-table" data-testid="review-table">
        <thead>
          <tr>
            <th>行</th>
            <th>字段</th>
            <th>保存值</th>
            <th>类型</th>
            <th>有效性</th>
            <th>来源</th>
            <th>原始输入</th>
            <th>失败原因</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in result.rows" :key="row.row_index + row.source_url">
            <tr
              v-for="(receipt, fieldIndex) in row.field_receipts"
              :key="row.row_index + receipt.field"
              :data-testid="`review-row-${row.row_index}-${receipt.field}`"
              :data-valid="row.valid"
            >
              <td v-if="fieldIndex === 0" :rowspan="row.field_receipts.length">{{ row.row_index }}</td>
              <td>{{ receipt.field }}</td>
              <td class="value-cell">{{ formatValue(receipt.value) }}</td>
              <td>{{ receipt.value_type }}</td>
              <td :class="receipt.status === 'valid' ? 'ok-text' : 'error-text'">
                {{ receipt.status === 'valid' ? '有效' : '无效' }}
              </td>
              <td>{{ receipt.source }}</td>
              <td>{{ formatValue(row.raw_fields[receipt.field]) }}</td>
              <td class="error-text">{{ receipt.reason || '—' }}</td>
            </tr>
          </template>
        </tbody>
      </table>

      <p class="page-foot">
        <span>回执编号：{{ result.review_id }}</span>
        <span>抽取规则：{{ result.extract_rule_id }} · 规则集：{{ result.rule_ids.join('、') }}</span>
      </p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import { reviewFieldInputs } from '@/api/backend-service'
import type { ReviewResult } from '@/api/backend-service'

const SAMPLE_INPUTS = [
  { 金额: 0, 上架标志: false, 备注: '零元赠品' },
  { 金额: 12.5, 上架标志: 'false', 备注: '常规商品' },
  { 金额: '', 上架标志: true, 备注: '待补价商品' },
]

const inputs = ref<Record<string, unknown>[]>([...SAMPLE_INPUTS])
const inputTexts = ref<string[]>(SAMPLE_INPUTS.map((item) => JSON.stringify(item)))
const extractRuleId = ref('CONT-REVIEW-001')
const result = ref<ReviewResult | null>(null)
const loading = ref(false)
const errorMessage = ref('')
const validityRule = ref('0 / false 视为合法值；空字符串或空值才视为真实缺失，并列出缺失字段来源。')

function resetInputs() {
  inputs.value = SAMPLE_INPUTS.map((item) => ({ ...item }))
  inputTexts.value = SAMPLE_INPUTS.map((item) => JSON.stringify(item))
}

function formatValue(value: unknown): string {
  if (value === '') return '""（空字符串）'
  if (value === null || value === undefined) return 'null'
  if (typeof value === 'boolean') return String(value)
  if (typeof value === 'number') return `${value}`
  return String(value)
}

async function runReview() {
  errorMessage.value = ''
  try {
    inputs.value = inputTexts.value.map((text, index) => {
      const parsed = JSON.parse(text)
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        throw new Error(`第 ${index + 1} 行必须是 JSON 对象`)
      }
      return parsed as Record<string, unknown>
    })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '输入不是合法 JSON'
    return
  }
  loading.value = true
  try {
    result.value = await reviewFieldInputs(inputs.value, extractRuleId.value)
    validityRule.value = result.value.validity_rule
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复核失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
textarea {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
}
.value-cell {
  font-family: 'JetBrains Mono', monospace;
}
.ok-text {
  color: #067647;
}
</style>
