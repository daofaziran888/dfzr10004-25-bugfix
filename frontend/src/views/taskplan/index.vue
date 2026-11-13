<template>
  <section class="page" data-module="taskplan">
    <header class="page-head">
      <div>
        <h2>采集任务编排管理</h2>
        <p class="page-desc">维护采集计划的站点配置、频率设置与资源分配。启动/重新排队时按目标站点解析唯一生效频率策略并锁定额度，规则停用后计划自动进入待重新排队。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记采集计划</button>
        <button class="btn" type="button" @click="exportRows">导出采集任务编排清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <!-- 排队待办：频率策略停用后这里跟着刷新 -->
    <div v-if="requeueRows.length" class="requeue-panel">
      <h4>待重新排队清单（{{ requeueRows.length }}）</h4>
      <ul>
        <li v-for="item in requeueRows" :key="String(item.id)">
          <span>{{ item.计划编号 }} · {{ item.目标站点 }} · {{ item.资源配额 }}</span>
          <button class="link" type="button" @click="runAction('重新排队', item)">重新排队</button>
        </li>
      </ul>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>计划编号</span>
        <input v-model="filters.keyword" placeholder="按计划编号检索" />
      </label>
      <label class="filter-item">
        <span>计划状态</span>
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
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-requeue': row.status === '待重新排队' }">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openCreate">登记</button>
            <button
              v-for="action in availableActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集任务编排数据，可先登记采集计划</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集任务编排记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="formVisible" class="modal-mask" @click.self="closeForm">
      <form class="modal" @submit.prevent="submitForm">
        <h3>登记采集计划</h3>
        <label class="form-item">
          <span>计划编号 *</span>
          <input v-model="form.计划编号" placeholder="如 TASK-0004" />
        </label>
        <label class="form-item">
          <span>目标站点 *（须在站点目录内）</span>
          <select v-model="form.目标站点">
            <option value="">请选择站点</option>
            <option v-for="site in sites" :key="site.目标站点" :value="site.目标站点">{{ site.目标站点 }}</option>
          </select>
        </label>
        <label class="form-item">
          <span>采集频率 *</span>
          <input v-model="form.采集频率" placeholder="如 每日一轮" />
        </label>
        <label class="form-item">
          <span>数据范围</span>
          <input v-model="form.数据范围" placeholder="默认 全量" />
        </label>
        <label class="form-item">
          <span>优先级</span>
          <select v-model="form.优先级">
            <option>普通</option>
            <option>高</option>
            <option>低</option>
          </select>
        </label>
        <p v-if="formError" class="error-text">{{ formError }}</p>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="closeForm">取消</button>
          <button class="btn primary" type="submit">保存（草稿）</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type SiteInfo = { 目标站点: string }

const ENDPOINT = '/api/taskplan'
const columns = ['计划编号', '目标站点', '采集频率', '数据范围', '优先级', '资源配额', '预计用时', '计划状态']
const statuses = ['草稿', '运行中', '已暂停', '已完成', '待重新排队']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const sites = ref<SiteInfo[]>([])
const requeueRows = ref<Row[]>([])
const filters = reactive<{ keyword: string; status: string; site: string }>({ keyword: '', status: '', site: '' })

const stats = reactive([
  { label: '采集计划总数', value: 0 },
  { label: '运行中计划', value: 0 },
  { label: '待重新排队', value: 0 },
])

const formVisible = ref(false)
const formError = ref('')
const emptyForm = () => ({ 计划编号: '', 目标站点: '', 采集频率: '', 数据范围: '', 优先级: '普通' })
const form = reactive(emptyForm())

function availableActions(row: Row): string[] {
  switch (row.status) {
    case '草稿':
      return ['启动计划']
    case '运行中':
      return ['暂停计划']
    case '已暂停':
      return ['归档计划']
    case '待重新排队':
      return ['重新排队']
    default:
      return []
  }
}

function resetFilters() {
  filters.keyword = ''
  filters.status = ''
  filters.site = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  formError.value = ''
  Object.assign(form, emptyForm())
  formVisible.value = true
}

function closeForm() {
  formVisible.value = false
}

async function submitForm() {
  formError.value = ''
  try {
    const response = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: { ...form } }) })
    const payload = await response.json()
    if (!response.ok || payload.ok === false) {
      formError.value = payload.message ?? '登记失败'
      return
    }
    formVisible.value = false
    await reload()
  } catch (error) {
    formError.value = error instanceof Error ? error.message : '登记请求失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!response.ok || payload.ok === false) {
      errorMessage.value = payload.message ?? '采集任务编排动作未生效'
      return
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集任务编排操作失败'
  }
}

async function loadSites() {
  try {
    const response = await request('/api/throttle/sites')
    if (response.ok) {
      const payload = await response.json()
      sites.value = payload.items ?? []
    }
  } catch {
    // 目录加载失败不阻塞列表
  }
}

async function loadRequeueQueue() {
  try {
    const response = await request(`${ENDPOINT}/requeue-queue`)
    if (response.ok) {
      const payload = await response.json()
      requeueRows.value = payload.items ?? []
    }
  } catch {
    // 待办读取失败不阻塞主列表
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
      throw new Error('采集计划列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集任务编排列表读取失败'
  }
  await loadRequeueQueue()
  try {
    const all = await request(`${ENDPOINT}?size=200`)
    if (all.ok) {
      const payload = await all.json()
      const allRows: Row[] = payload.items ?? []
      stats[0].value = payload.total ?? allRows.length
      stats[1].value = allRows.filter((r) => r.status === '运行中').length
      stats[2].value = allRows.filter((r) => r.status === '待重新排队').length
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
.requeue-panel {
  background: #fff7ed;
  border: 1px solid #fdba74;
  border-radius: 8px;
  padding: 10px 14px;
  margin-bottom: 12px;
}
.requeue-panel h4 {
  margin: 0 0 6px;
  font-size: 14px;
  color: #9a3412;
}
.requeue-panel ul {
  margin: 0;
  padding-left: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.requeue-panel li {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
}
.row-requeue {
  background: #fff7ed;
}
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
  width: 520px;
  max-height: 88vh;
  overflow: auto;
}
.modal h3 {
  margin: 0 0 12px;
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
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}
</style>
