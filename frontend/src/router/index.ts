import { createRouter, createWebHistory } from 'vue-router'

import BatchStoreDetail from '@/views/BatchStoreDetail.vue'
import FieldCleanReview from '@/views/FieldCleanReview.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/field_clean/review', name: 'field-clean-review', component: FieldCleanReview },
    {
      path: '/batch_store/:batchIdParam/detail',
      name: 'batch-store-detail',
      component: BatchStoreDetail,
      props: true,
    },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
