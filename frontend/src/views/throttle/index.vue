<template>
  <section class="page" data-module="throttle">
    <header class="page-head">
      <div>
        <h2>采集频率控制管理</h2>
        <p class="page-desc">维护采集频率的请求间隔、日上限与优先级权重。同站点同时只允许一条生效策略，停用后计划侧额度立即失效。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记频率策略</button>
        <button class="btn" type="button" @click="exportRows">导出采集频率控制清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>策略编号</span>
        <input v-model="filters.keyword" placeholder="按策略编号检索" />
      </label>
      <label class="filter-item">
        <span>策略状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>目标站点</span>
        <select v-model="filters.site">
          <option value="">全部</option>
          <option v-for="site in sites" :key="site.目标站点" :value="site.目标站点">{{ site.目标站点 }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openEdit(row)">修改</button>
            <button
              v-for="action in availableActions(row)"
              :key="action"
              class="link"
              :class="{ danger: action === '启用策略' }"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集频率控制数据，可先登记频率策略</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集频率控制记录（与详情、导出口径一致）</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 登记 / 修改弹窗：保存条件直接写在字段提示里 -->
    <div v-if="formVisible" class="modal-mask" @click.self="closeForm">
      <form class="modal" @submit.prevent="submitForm">
        <h3>{{ editingId === null ? '登记频率策略' : `修改频率策略 #${editingId}` }}</h3>
        <p class="form-hint">
          保存条件：请求间隔 100~60000 毫秒且不低于站点最小间隔；日请求上限 1~100000 且不超过站点日配额；
          并发数量 1~50 且不超过站点并发配额；优先级权重 1~100 的正整数。不符合条件不允许保存。
        </p>
        <label class="form-item">
          <span>策略编号 *</span>
          <input v-model="form.策略编号" :disabled="editingId !== null" placeholder="如 THRO-0004" />
        </label>
        <label class="form-item">
          <span>目标站点 *</span>
          <select v-model="form.目标站点" @change="onSiteChange">
            <option value="">请选择站点目录中的站点</option>
            <option v-for="site in sites" :key="site.目标站点" :value="site.目标站点">
              {{ site.目标站点 }}（日配额 {{ site.日请求硬配额 }} / 并发 {{ site.并发硬配额 }} / 最小间隔 {{ site.最小请求间隔毫秒 }}ms）
            </option>
          </select>
        </label>
        <label class="form-item">
          <span>请求间隔（毫秒）*</span>
          <input v-model.number="form.请求间隔" type="number" min="100" max="60000" />
        </label>
        <label class="form-item">
          <span>日请求上限 *</span>
          <input v-model.number="form.日请求上限" type="number" min="1" max="100000" />
        </label>
        <label class="form-item">
          <span>并发数量 *</span>
          <input v-model.number="form.并发数量" type="number" min="1" max="50" />
        </label>
        <label class="form-item">
          <span>排队策略</span>
          <select v-model="form.排队策略">
            <option>公平排队</option>
            <option>权重优先</option>
            <option>先来先服务</option>
          </select>
        </label>
        <label class="form-item">
          <span>优先级权重 *（1~100）</span>
          <input v-model.number="form.优先级权重" type="number" min="1" max="100" />
        </label>
        <p v-if="formErrors.length" class="error-text form-errors">
          <span v-for="(msg, idx) in formErrors" :key="idx">· {{ msg }}<br /></span>
        </p>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="closeForm">取消</button>
          <button class="btn primary" type="submit">{{ editingId === null ? '保存（待配置）' : '保存修改' }}</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>
type SiteInfo = {
  目标站点: string
  站点域名: string
  日请求硬配额: number
  并发硬配额: number
  最小请求间隔毫秒: number
}

const ENDPOINT = '/api/throttle'
const columns = ['策略编号', '目标站点', '请求间隔', '日请求上限', '并发数量', '排队策略', '优先级权重', '是否启用']
const statuses = ['待配置', '候选', '生效', '停用']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const sites = ref<SiteInfo[]>([])
const filters = reactive<{ keyword: string; status: string; site: string }>({ keyword: '', status: '', site: '' })

const stats = reactive([
  { label: '频率策略总数', value: 0 },
  { label: '候选策略', value: 0 },
  { label: '生效策略', value: 0 },
])

const formVisible = ref(false)
const editingId = ref<number | null>(null)
const formErrors = ref<string[]>([])
const emptyForm = () => ({
  策略编号: '',
  目标站点: '',
  请求间隔: 500,
  日请求上限: 1000,
  并发数量: 2,
  排队策略: '公平排队',
  优先级权重: 10,
})
const form = reactive(emptyForm())

function resetFilters() {
  filters.keyword = ''
  filters.status = ''
  filters.site = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function availableActions(row: Row): string[] {
  switch (row.status) {
    case '待配置':
      return ['提交校验']
    case '候选':
      return ['启用策略']
    case '生效':
      return ['停用策略']
    default:
      return []
  }
}

async function loadSites() {
  try {
    const response = await request(`${ENDPOINT}/sites`)
    if (response.ok) {
      const payload = await response.json()
      sites.value = payload.items ?? []
    }
  } catch {
    // 目录加载失败不阻塞列表，登记时后端还会再校验
  }
}

function openCreate() {
  editingId.value = null
  formErrors.value = []
  Object.assign(form, emptyForm())
  formVisible.value = true
}

function openEdit(row: Row) {
  if (row.status === '停用') {
    errorMessage.value = '已停用策略不能修改，请登记新策略'
    return
  }
  editingId.value = Number(row.id)
  formErrors.value = []
  Object.assign(form, {
    策略编号: String(row.策略编号 ?? ''),
    目标站点: String(row.目标站点 ?? ''),
    请求间隔: Number(row.请求间隔),
    日请求上限: Number(row.日请求上限),
    并发数量: Number(row.并发数量),
    排队策略: String(row.排队策略 ?? '公平排队'),
    优先级权重: Number(row.优先级权重),
  })
  formVisible.value = true
}

function closeForm() {
  formVisible.value = false
}

function onSiteChange() {
  // 站点切换后不自动改数值，由保存校验统一给出额度冲突说明
}

async function submitForm() {
  formErrors.value = []
  const url = editingId.value === null ? ENDPOINT : `${ENDPOINT}/${editingId.value}`
  const method = editingId.value === null ? 'POST' : 'PUT'
  try {
    const response = await request(url, { method, body: JSON.stringify({ values: { ...form } }) })
    const payload = await response.json()
    if (!response.ok || payload.ok === false) {
      formErrors.value = [payload.message ?? '保存失败']
      return
    }
    formVisible.value = false
    errorMessage.value = ''
    await reload()
  } catch (error) {
    formErrors.value = [error instanceof Error ? error.message : '保存请求失败']
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  const body: Record<string, unknown> = { action }
  // 同站点冲突时，二次确认后显式指定以当前候选为准
  if (action === '启用策略') {
    const confirmReplace = window.confirm(
      `确认启用策略「${row.策略编号}」？\n若同站点已有生效策略，将以本策略为准，旧策略自动停用。`,
    )
    if (!confirmReplace) return
    body.replace_policy = true
  }
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
    const payload = await response.json()
    if (!response.ok || payload.ok === false) {
      errorMessage.value = payload.message ?? '采集频率控制动作未生效'
      return
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集频率控制操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (filters.keyword) query.set('keyword', filters.keyword)
  if (filters.status) query.set('status', filters.status)
  if (filters.site) query.set('site', filters.site)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('频率策略列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集频率控制列表读取失败'
  }
  // 统计卡片实时计算，与列表/详情同一份数据
  try {
    const all = await request(`${ENDPOINT}?size=200`)
    if (all.ok) {
      const payload = await all.json()
      const allRows: Row[] = payload.items ?? []
      stats[0].value = payload.total ?? allRows.length
      stats[1].value = allRows.filter((r) => r.status === '候选').length
      stats[2].value = allRows.filter((r) => r.status === '生效').length
    }
  } catch {
    // 统计失败不影响主列表
  }
}

onMounted(() => {
  void loadSites()
  void reload()
})
</script>

<style scoped>
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 20;
}
.modal {
  background: #fff;
  border-radius: 10px;
  padding: 20px 24px;
  width: 560px;
  max-height: 88vh;
  overflow: auto;
}
.modal h3 {
  margin: 0 0 8px;
}
.form-hint {
  font-size: 12px;
  color: var(--muted);
  background: #f1f5f9;
  border-radius: 6px;
  padding: 8px 10px;
}
.form-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 10px;
  font-size: 13px;
}
.form-item input,
.form-item select {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
}
.form-errors {
  font-size: 12px;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}
.link.danger {
  color: #b42318;
}
</style>
