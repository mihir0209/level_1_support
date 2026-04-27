/* ════════════════════════════════════════════════════════════════
   L1 Support AI — Alpine.js Application Logic
   All sections: Demo, Sessions, Monitoring, Brands, Graph,
   Knowledge Base, Ontology, Integrations, Voice
═══════════════════════════════════════════════════════════════ */

// ── Toast notifications ──────────────────────────────────────────
function toasts() {
  return {
    items: [],
    init() { window.showToast = (msg, cls = 'toast-info') => {
      const id = Date.now();
      this.items.push({ id, msg, cls });
      setTimeout(() => this.items = this.items.filter(t => t.id !== id), 4000);
    }; },
  };
}

// ── Main Application ─────────────────────────────────────────────
function app() {
  return {
    // ── Core state ─────────────────────────────────────────────
    activeTab: 'demo',
    theme: localStorage.getItem('theme') || 'light',
    healthOk: false,
    adminKey: 'l1support-demo-key-2026',

    navTabs: [
      { id: 'demo',         label: 'Support Demo' },
      { id: 'sessions',     label: 'Sessions' },
      { id: 'monitoring',  label: 'Monitoring' },
      { id: 'brands',       label: 'Brands' },
      { id: 'graph',        label: 'Graph' },
      { id: 'kb',            label: 'Knowledge Base' },
      { id: 'ontology',      label: 'Ontology' },
      { id: 'integrations',  label: 'Integrations' },
      { id: 'voice',         label: 'Voice' },
    ],

    // ── DEMO ────────────────────────────────────────────────────
    brands: [],
    selectedBrand: 'boohoo_uk',
    selectedChannel: 'chat',
    selectedCustomer: '',
    customers: [],
    currentSession: null,
    messages: [],
    chatInput: '',
    isLoading: false,
    lastResponse: null,
    customerContext: null,

    flowSteps: [
      { label: 'Intent Classification', desc: 'Fast model classifies query type', done: false, active: false },
      { label: 'Entity Extraction',       desc: 'Order ID, SKU, reason extraction', done: false, active: false },
      { label: 'Knowledge Retrieval',     desc: 'GraphRAG → resolution paths',      done: false, active: false },
      { label: 'Policy Resolution',      desc: 'Temporal-aware policy lookup',      done: false, active: false },
      { label: 'LLM Response',           desc: 'Full ContextPackage assembled',     done: false, active: false },
      { label: 'Resolution Check',       desc: 'Auto-resolve or escalate to L2',  done: false, active: false },
    ],

    escalationRules: [
      { condition: 'Confidence < 45%',           action: 'Escalate to L2 human agent', active: false },
      { condition: 'Resolution attempts ≥ 10',  action: 'Escalate to L2 human agent', active: false },
      { condition: 'Turn count ≥ 10',            action: 'Escalate to L2 human agent', active: false },
      { condition: 'Customer asks for human',   action: 'Immediate L2 escalation',   active: false },
      { condition: 'Refund overdue > 21 days', action: 'Escalate to L2 human agent', active: false },
    ],

    quickChips: [
      { text: 'Where is my order?' },
      { text: 'I want to return an item' },
      { text: 'My item arrived damaged' },
      { text: 'Apply a discount code' },
      { text: 'Cancel my order' },
      { text: 'Track my delivery' },
    ],

    // ── SESSIONS ────────────────────────────────────────────────
    sessions: [],
    filteredSessions: [],
    sessTab: 'list',
    sessBrandFilter: '',
    sessionsSubTabs: [
      { id: 'list',   label: 'Active Sessions' },
      { id: 'detail', label: 'Session Detail' },
    ],
    selectedSessId: '',
    sessionDetail: null,

    // ── MONITORING ──────────────────────────────────────────────
    services: [],
    metrics: [],
    graphStats: {},
    intentDist: [],
    monTab: 'overview',
    monSubTabs: [
      { id: 'overview', label: 'System Overview' },
      { id: 'jobs',      label: 'Ingestion Jobs' },
    ],
    ingestionJobs: [],

    // ── BRANDS ──────────────────────────────────────────────────
    brandTab: 'list',
    brandSubTabs: [
      { id: 'list',   label: 'All Brands' },
      { id: 'manage', label: 'Brand Management' },
    ],
    mgmtBrandId: '',
    collectMaxUrls: 200,
    collectConcurrency: 5,
    collectLoading: false,
    collectStatus: null,
    probeResult: null,
    brandOntology: null,
    seedLoading: false,
    seedResult: null,
    uploadLoading: false,

    // ── GRAPH ───────────────────────────────────────────────────
    graphTab: 'summary',
    graphSubTabs: [
      { id: 'summary', label: 'Graph Summary' },
      { id: 'cypher',  label: 'Cypher Query' },
      { id: 'issues',  label: 'Issue Types' },
    ],
    graphData: {},
    graphMetrics: [],
    cypherQuery: '',
    cypherLoading: false,
    cypherResult: null,
    cypherExamples: [
      { label: 'All brands',        query: "MATCH (b:Brand) RETURN b.name, b.tier LIMIT 20" },
      { label: 'Return policies',   query: "MATCH (b:Brand)-[:HAS_POLICY {type:'return'}]->(p:ReturnPolicy) RETURN b.name, p.return_window_days LIMIT 20" },
      { label: 'FAQ count',         query: "MATCH (f:FAQ) RETURN f.brand_id, count(f) as c ORDER BY c DESC" },
      { label: 'Top resolutions',   query: "MATCH (r:Resolution) RETURN r.name, r.success_rate ORDER BY r.success_rate DESC LIMIT 10" },
      { label: 'Entity types',      query: "CALL db.labels() YIELD label RETURN label ORDER BY label" },
    ],
    graphIssues: [],

    // ── KB ───────────────────────────────────────────────────────
    kbTab: 'faqs',
    kbSubTabs: [
      { id: 'faqs',    label: 'FAQs' },
      { id: 'policies', label: 'Policies' },
      { id: 'chunks',  label: 'pgvector Chunks' },
    ],
    kbBrandFilter: '',
    kbCategoryFilter: '',
    kbCategories: ['order','return','payment','delivery','account','promotions'],
    kbPolicies: [],
    policyBrandFilter: '',
    kbFaqs: [],
    kbChunks: [],
    chunkBrandFilter: '',
    chunkTypeFilter: '',
    chunkTypes: ['faq','policy','knowledge'],
    kbLoading: false,

    // ── ONTOLOGY ─────────────────────────────────────────────────
    ontLabels: [],
    ontRels: [],
    ontProps: [],
    ontTypes: [],
    ontologyProposals: [],
    proposalStatus: '',
    migrationResult: null,
    ontBrandFilter: '',

    // ── INTEGRATIONS ─────────────────────────────────────────────
    integrations: [],
    newInt: { name: '', url: '', description: '', type: 'mcp', authHeader: '' },
    intProbeResult: null,

    // ── VOICE ───────────────────────────────────────────────────
    voiceCalls: [],
    voiceBrand: 'boohoo_uk',
    voiceLoading: false,
    voiceResult: null,

    // ════════════════════════════════════════════════════════════════
    // LIFECYCLE
    // ════════════════════════════════════════════════════════════════
    async init() {
      this.applyTheme();
      await this.loadBrands();
      await this.checkHealth();
      this.refreshMonitoring();
      setInterval(() => this.refreshMonitoring(), 15000);
    },

    applyTheme() {
      document.documentElement.classList.toggle('dark', this.theme === 'dark');
    },

    toggleTheme() {
      this.theme = this.theme === 'light' ? 'dark' : 'light';
      localStorage.setItem('theme', this.theme);
      this.applyTheme();
    },

    // ════════════════════════════════════════════════════════════════
    // API HELPER — method-agnostic, handles JSON body + query params
    // ════════════════════════════════════════════════════════════════
    // opts: { method?, body?, headers?, noJson?, formData? }
    async api(path, opts = {}) {
      const base = window.location.origin;
      const method = opts.method || 'GET';
      const hasBody = method !== 'GET' && opts.body !== undefined;
      const headers = { ...(opts.headers || {}) };
      let body = opts.body;

      if (path.startsWith('/admin')) {
        headers['X-ADMIN-KEY'] = this.adminKey;
      }
      if (path.startsWith('/api/v1')) {
        headers['X-API-KEY'] = this.adminKey;
      }

      if (hasBody && !opts.formData) {
        headers['Content-Type'] = 'application/json';
      } else if (opts.formData) {
        delete headers['Content-Type'];
      }

      const fetchOpts = { method, headers };
      if (hasBody && !opts.formData) fetchOpts.body = body;
      if (opts.formData) fetchOpts.body = opts.formData;

      const res = await fetch(base + path, fetchOpts);
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      if (opts.noJson) return res;
      return res.json();
    },

    toast(msg, cls = 'toast-info') { if (window.showToast) window.showToast(msg, cls); },

    // ════════════════════════════════════════════════════════════════
    // BRANDS
    // ════════════════════════════════════════════════════════════════
    async loadBrands() {
      try { const d = await this.api('/admin/brands'); this.brands = d.brands || []; }
      catch (e) { this.toast('Failed to load brands: ' + e.message, 'toast-error'); }
    },

    onBrandChange() {
      this.customers = [];
      this.selectedCustomer = '';
      this.customerContext = null;
    },

    getBrandName(id) {
      const b = this.brands.find(b => b.brand_id === id);
      return b ? b.name : (id || '—');
    },

    // ════════════════════════════════════════════════════════════════
    // HEALTH
    // ════════════════════════════════════════════════════════════════
    async checkHealth() {
      try {
        const h = await this.api('/health');
        this.healthOk = h.status === 'ok' || h.status === 'healthy';
        this.services = [
          { name: 'L1 API',        type: 'service', status: 'up', detail: window.location.origin },
          { name: 'FalkorDB',       type: 'graph',   status: h.falkordb === 'ok' ? 'up' : 'down', detail: h.falkordb || 'unavailable' },
          { name: 'Redis',          type: 'cache',   status: h.redis === 'ok' ? 'up' : 'down',   detail: h.redis || 'unavailable' },
          { name: 'Postgres',       type: 'db',      status: 'up',  detail: 'pgvector store' },
          { name: 'Chatwoot',       type: 'l2',      status: 'up',  detail: 'L2 escalation channel' },
        ];
      } catch (e) { this.healthOk = false; }
    },

    // ════════════════════════════════════════════════════════════════
    // DEMO — SESSION
    // ════════════════════════════════════════════════════════════════
    async startSession() {
      if (this.currentSession) {
        this.messages = [];
        this.lastResponse = null;
        this.customerContext = null;
        this.currentSession = null;
        this.resetFlow();
        return;
      }
      this.isLoading = true;
      try {
        // Seed demo data (POST /admin/brands/{brand_id}/demo-data)
        try {
          await this.api('/admin/brands/' + this.selectedBrand + '/demo-data', {
            method: 'POST',
          });
        } catch (e) { this.toast('Seed failed: ' + e.message, 'toast-error'); }
        // Load customers for this brand
        try {
          const cd = await this.api('/api/v1/customers?brand_id=' + this.selectedBrand);
          this.customers = (cd.customers || []).map(c => ({ ...c, id: c.customer_id || c.id }));
          if (!this.customers.length) this.toast('No customers found for ' + this.selectedBrand, 'toast-warning');
        } catch (e) { this.toast('Customer load failed: ' + e.message, 'toast-error'); }

        const data = await this.api('/session/new', {
          method: 'POST',
          body: JSON.stringify({ brand: this.selectedBrand, channel: this.selectedChannel, customer_id: this.selectedCustomer || null }),
        });
        this.currentSession = data;
        this.messages = [];
        this.lastResponse = null;
        this.resetFlow();
        this.loadCustomerContext();
        this.messages.push({ role: 'system', title: 'Session Started', content: 'Connected to ' + data.brand_name + '. ' + (this.selectedCustomer ? 'Customer context loaded.' : 'Guest session.') + ' Describe your issue.', time: new Date().toLocaleTimeString() });
      } catch (e) {
        this.messages.push({ role: 'system', title: 'Error', content: 'Failed: ' + e.message, time: new Date().toLocaleTimeString() });
      } finally { this.isLoading = false; }
    },

    async loadCustomerContext() {
      if (!this.selectedCustomer) { this.customerContext = null; return; }
      try {
        const d = await this.api('/api/v1/customers/' + this.selectedCustomer);
        this.customerContext = d;
      } catch (_) { this.customerContext = null; }
    },

    async sendMessage() {
      if (!this.chatInput.trim() || !this.currentSession || this.isLoading) return;
      const msg = this.chatInput.trim();
      this.chatInput = '';
      this.isLoading = true;
      this.messages.push({ role: 'user', content: msg, time: new Date().toLocaleTimeString() });
      this.activateFlow(0);
      try {
        const data = await this.api('/chat', {
          method: 'POST',
          body: JSON.stringify({ session_id: this.currentSession.session_id, message: msg, customer_id: this.selectedCustomer || null }),
        });
        this.lastResponse = data;
        this.messages.push({ role: 'agent', content: data.response, intent: data.intent, time: new Date().toLocaleTimeString() });
        this.activateFlow(4);
        if (data.is_resolved) { this.markFlowDone(5); }
        this.$nextTick(() => { const el = document.getElementById('chat-bottom'); if (el) el.scrollIntoView({ behavior: 'smooth' }); });
      } catch (e) {
        this.messages.push({ role: 'agent', content: 'I encountered an error. Please try again.', time: new Date().toLocaleTimeString() });
      } finally { this.isLoading = false; }
    },

    async sendQuick(text) { this.chatInput = text; await this.sendMessage(); },

    resetDemo() {
      this.currentSession = null; this.messages = []; this.lastResponse = null;
      this.customerContext = null; this.customers = []; this.resetFlow();
    },

    resetFlow() { this.flowSteps.forEach(s => { s.done = false; s.active = false; }); },

    activateFlow(idx) {
      this.flowSteps.forEach((s, i) => { s.active = i === idx; if (i < idx) s.done = true; });
    },

    markFlowDone(idx) {
      this.flowSteps.forEach((s, i) => { if (i <= idx) s.done = true; s.active = false; });
    },

    formatMsg(content, role) {
      if (!content) return '';
      let html = content.replace(/\n/g, '<br/>')
        .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
        .replace(/`(.*?)`/g, '<code class="mono text-xs bg-surface-100 dark:bg-surface-700 px-1 py-0.5 rounded">$1</code>');
      if (role === 'agent') {
        html = html.replace(/\[TOOL: ([^\]]+)\]/g,
          '<span class="inline-flex items-center gap-1 my-1 px-2 py-1 bg-brand-50 dark:bg-brand-900/30 border border-brand-200 rounded text-xs text-brand-700 dark:text-brand-300 mono">TOOL: $1</span>');
        html = html.replace(/\[RESOLVED\]/gi,
          '<span class="inline-flex items-center gap-1 my-1 px-2 py-1 bg-green-50 dark:bg-green-900/30 border border-green-200 rounded text-xs text-green-700 dark:text-green-300">RESOLVED ✓</span>');
      }
      return html;
    },

    // ════════════════════════════════════════════════════════════════
    // SESSIONS
    // ════════════════════════════════════════════════════════════════
    async refreshSessions() {
      try {
        const d = await this.api('/admin/sessions');
        this.sessions = d.sessions || [];
        this.applySessionsFilter();
      } catch (e) { this.toast('Sessions load failed: ' + e.message, 'toast-error'); }
    },

    applySessionsFilter() {
      if (!this.sessBrandFilter) { this.filteredSessions = this.sessions; return; }
      this.filteredSessions = this.sessions.filter(s => s.brand_id === this.sessBrandFilter);
    },

    async openSessionDetail(id) {
      this.selectedSessId = id;
      this.sessTab = 'detail';
      try { this.sessionDetail = await this.api('/admin/sessions/' + id); }
      catch (e) { this.sessionDetail = null; this.toast('Failed to load session: ' + e.message, 'toast-error'); }
    },

    // ════════════════════════════════════════════════════════════════
    // MONITORING
    // ════════════════════════════════════════════════════════════════
    async refreshMonitoring() {
      try {
        const [ov, rels, summary, jobs] = await Promise.allSettled([
          this.api('/admin/overview'),
          this.api('/admin/graph/relationships'),
          this.api('/admin/graph/summary'),
          this.api('/admin/jobs'),
        ]);

        if (ov.status === 'fulfilled') {
          const o = ov.value;
          this.metrics = [
            { label: 'Registered Brands', value: o.registered_brands || 0, sub: 'across platform' },
            { label: 'Active Sessions',   value: o.active_sessions || 0,    sub: 'in Redis' },
            { label: 'Graph Nodes',        value: (o.knowledge_graph?.nodes_by_label || []).reduce((a, n) => a + n.count, 0), sub: 'in FalkorDB' },
            { label: 'KB Chunks',          value: Object.values(o.postgres_kb || {}).reduce((a, c) => a + c, 0), sub: 'in pgvector' },
          ];
        }

        if (rels.status === 'fulfilled') {
          this.graphStats.relationships = rels.value.relationships || [];
        }

        if (summary.status === 'fulfilled') {
          this.graphStats.nodes = summary.value.nodes || [];
          this.graphStats.maxCount = Math.max(...(summary.value.nodes || []).map(n => n.count), 1);
        }

        if (jobs.status === 'fulfilled') {
          this.ingestionJobs = jobs.value.jobs || [];
        }

        await this.refreshSessions();
        this.intentDist = [
          { name: 'WISMO',    count: Math.floor(Math.random() * 40) + 10 },
          { name: 'Returns', count: Math.floor(Math.random() * 30) + 5 },
          { name: 'Refund',  count: Math.floor(Math.random() * 20) + 3 },
          { name: 'Payment', count: Math.floor(Math.random() * 15) + 2 },
          { name: 'Account', count: Math.floor(Math.random() * 10) + 1 },
          { name: 'Promos',  count: Math.floor(Math.random() * 8) + 1 },
          { name: 'Other',    count: Math.floor(Math.random() * 5) + 1 },
        ];
      } catch (e) { /* silent */ }
    },

    async refreshJobs() { await this.refreshMonitoring(); },

    async triggerIngest() {
      try {
        await this.api('/admin/ingest', {
          method: 'POST',
          body: JSON.stringify({ brand: this.selectedBrand }),
        });
        this.toast('Ingest triggered for ' + this.selectedBrand, 'toast-success');
        await this.refreshJobs();
      } catch (e) { this.toast('Ingest failed: ' + e.message, 'toast-error'); }
    },

    // ════════════════════════════════════════════════════════════════
    // BRANDS MANAGEMENT
    // ════════════════════════════════════════════════════════════════
    selectBrandForMgmt(brandId) {
      this.mgmtBrandId = brandId;
      this.brandTab = 'manage';
      this.probeResult = null;
      this.collectStatus = null;
      this.brandOntology = null;
      this.seedResult = null;
    },

    async probeBrand(brandId) {
      if (!brandId) return;
      try { this.probeResult = await this.api('/admin/brands/' + brandId + '/probe'); }
      catch (e) { this.toast('Probe failed: ' + e.message, 'toast-error'); }
    },

    async triggerCollect() {
      if (!this.mgmtBrandId) return;
      this.collectLoading = true;
      try {
        await this.api('/admin/brands/' + this.mgmtBrandId + '/collect', {
          method: 'POST',
          body: JSON.stringify({ max_urls: this.collectMaxUrls, concurrency: this.collectConcurrency }),
        });
        this.toast('Collection started for ' + this.mgmtBrandId, 'toast-success');
        this.loadCollectStatus();
      } catch (e) { this.toast('Collect failed: ' + e.message, 'toast-error'); }
      finally { this.collectLoading = false; }
    },

    async loadCollectStatus() {
      if (!this.mgmtBrandId) return;
      try { this.collectStatus = await this.api('/admin/brands/' + this.mgmtBrandId + '/collect/status'); }
      catch (e) { /* not started yet */ }
    },

    async seedDemoData(brandId) {
      if (!brandId) return;
      this.seedLoading = true;
      try {
        this.seedResult = await this.api('/admin/brands/' + brandId + '/demo-data', {
          method: 'POST',
        });
        this.toast('Demo data seeded for ' + brandId, 'toast-success');
      } catch (e) { this.toast('Seed failed: ' + e.message, 'toast-error'); }
      finally { this.seedLoading = false; }
    },

    async uploadDocs() {
      if (!this.mgmtBrandId) return;
      const input = document.getElementById('doc-upload');
      if (!input || !input.files.length) { this.toast('No files selected', 'toast-info'); return; }
      this.uploadLoading = true;
      try {
        const form = new FormData();
        for (const f of input.files) form.append('files', f);
        await fetch(window.location.origin + '/admin/brands/' + this.mgmtBrandId + '/documents/upload', {
          method: 'POST',
          headers: { 'X-ADMIN-KEY': this.adminKey },
          body: form,
        });
        this.toast('Documents uploaded and ingested', 'toast-success');
        input.value = '';
      } catch (e) { this.toast('Upload failed: ' + e.message, 'toast-error'); }
      finally { this.uploadLoading = false; }
    },

    async loadBrandOntology(brandId) {
      if (!brandId) return;
      try { this.brandOntology = await this.api('/admin/ontology/brand/' + brandId); }
      catch (e) { this.toast('Ontology load failed: ' + e.message, 'toast-error'); }
    },

    // ════════════════════════════════════════════════════════════════
    // GRAPH
    // ════════════════════════════════════════════════════════════════
    async loadGraphData() {
      try {
        const [summary, rels] = await Promise.allSettled([
          this.api('/admin/graph/summary'),
          this.api('/admin/graph/relationships'),
        ]);
        if (summary.status === 'fulfilled') {
          this.graphData.nodes = summary.value.nodes || [];
          this.graphData.maxCount = Math.max(...(summary.value.nodes || []).map(n => n.count), 1);
        }
        if (rels.status === 'fulfilled') {
          this.graphData.rels = rels.value.relationships || [];
        }
        this.graphMetrics = [
          { label: 'Total Nodes', value: summary.value?.total_nodes || 0 },
          { label: 'Relationships', value: rels.value?.relationships?.length || 0 },
          { label: 'Brands', value: this.brands.length },
        ];
      } catch (e) { this.toast('Graph load failed', 'toast-error'); }
    },

    async runCypher() {
      if (!this.cypherQuery.trim()) return;
      this.cypherLoading = true;
      try { this.cypherResult = await this.api('/admin/graph/query?q=' + encodeURIComponent(this.cypherQuery)); }
      catch (e) { this.toast('Query failed: ' + e.message, 'toast-error'); this.cypherResult = null; }
      finally { this.cypherLoading = false; }
    },

    async loadIssues() {
      try { const d = await this.api('/admin/graph/issues'); this.graphIssues = d.issues || []; }
      catch (e) { this.graphIssues = []; }
    },

    // ════════════════════════════════════════════════════════════════
    // KNOWLEDGE BASE
    // ════════════════════════════════════════════════════════════════
    async loadFaqs() {
      this.kbLoading = true;
      try {
        let path = '/admin/kb/faqs';
        const params = [];
        if (this.kbBrandFilter) params.push('brand_id=' + this.kbBrandFilter);
        if (this.kbCategoryFilter) params.push('category=' + this.kbCategoryFilter);
        if (params.length) path += '?' + params.join('&');
        const d = await this.api(path);
        this.kbFaqs = d.faqs || [];
      } catch (e) { this.kbFaqs = []; }
      finally { this.kbLoading = false; }
    },

    async loadPolicies() {
      this.kbLoading = true;
      try {
        let path = '/admin/kb/policies';
        if (this.policyBrandFilter) path += '?brand_id=' + this.policyBrandFilter;
        const d = await this.api(path);
        this.kbPolicies = d.policies || [];
      } catch (e) { this.kbPolicies = []; }
      finally { this.kbLoading = false; }
    },

    async loadChunks() {
      this.kbLoading = true;
      try {
        const params = [];
        if (this.chunkBrandFilter) params.push('brand_id=' + this.chunkBrandFilter);
        if (this.chunkTypeFilter) params.push('chunk_type=' + this.chunkTypeFilter);
        const path = '/admin/kb/pgvector/chunks' + (params.length ? '?' + params.join('&') : '');
        const d = await this.api(path);
        this.kbChunks = d.chunks || [];
      } catch (e) { this.kbChunks = []; }
      finally { this.kbLoading = false; }
    },

    // ════════════════════════════════════════════════════════════════
    // ONTOLOGY
    // ════════════════════════════════════════════════════════════════
    async loadOntology() {
      try {
        const d = await this.api('/admin/ontology');
        this.ontLabels = d.node_labels || [];
        this.ontRels   = d.relationship_types || [];
        this.ontProps   = d.property_keys || [];
      } catch (_) {}

      try {
        const t = await this.api('/admin/ontology/types');
        this.ontTypes = t.types || [];
      } catch (_) {}

      await this.loadProposals();
    },

    async loadProposals() {
      try {
        const path = this.proposalStatus ? '/admin/ontology/proposals?status=' + this.proposalStatus : '/admin/ontology/proposals';
        const d = await this.api(path);
        this.ontologyProposals = d.proposals || [];
      } catch (_) { this.ontologyProposals = []; }
    },

    async applyProposal(proposalId) {
      if (!proposalId) return;
      try {
        await this.api('/admin/ontology/proposals/' + proposalId + '/apply', { method: 'POST' });
        this.toast('Proposal ' + proposalId + ' applied', 'toast-success');
        await this.loadProposals();
      } catch (e) { this.toast('Apply failed: ' + e.message, 'toast-error'); }
    },

    async runMigration(dryRun = true) {
      try {
        this.migrationResult = await this.api('/admin/ontology/migrate-nodes?dry_run=' + dryRun);
        this.toast(dryRun ? 'Dry run complete' : 'Migration applied', 'toast-success');
      } catch (e) { this.toast('Migration failed: ' + e.message, 'toast-error'); }
    },

    // ════════════════════════════════════════════════════════════════
    // INTEGRATIONS
    // ════════════════════════════════════════════════════════════════
    async loadIntegrations() {
      try { const d = await this.api('/integrations/mcp'); this.integrations = d.integrations || []; }
      catch (_) { this.integrations = []; }
    },

    async registerIntegration() {
      if (!this.newInt.name || !this.newInt.url) return;
      try {
        const headers = {};
        if (this.newInt.authHeader) headers['Authorization'] = 'Bearer ...';
        await this.api('/integrations/mcp', {
          method: 'POST',
          body: JSON.stringify({
            name: this.newInt.name,
            url: this.newInt.url,
            description: this.newInt.description,
            type: this.newInt.type,
            auth_headers: this.newInt.authHeader ? { [this.newInt.authHeader]: '***' } : {},
          }),
        });
        this.toast('Integration registered: ' + this.newInt.name, 'toast-success');
        this.newInt = { name: '', url: '', description: '', type: 'mcp', authHeader: '' };
        await this.loadIntegrations();
      } catch (e) { this.toast('Register failed: ' + e.message, 'toast-error'); }
    },

    async probeIntegration(name) {
      if (!name) return;
      try { this.intProbeResult = await this.api('/integrations/mcp/' + name + '/probe'); }
      catch (e) { this.toast('Probe failed: ' + e.message, 'toast-error'); }
    },

    async removeIntegration(name) {
      if (!name) return;
      try {
        await this.api('/integrations/mcp/' + name, { method: 'DELETE' });
        this.toast('Removed: ' + name, 'toast-info');
        await this.loadIntegrations();
      } catch (e) { this.toast('Remove failed: ' + e.message, 'toast-error'); }
    },

    // ════════════════════════════════════════════════════════════════
    // VOICE
    // ════════════════════════════════════════════════════════════════
    async loadVoiceCalls() {
      try { const d = await this.api('/voice/active'); this.voiceCalls = d.calls || []; }
      catch (_) { this.voiceCalls = []; }
    },

    async startVoiceCall() {
      this.voiceLoading = true;
      try {
        this.voiceResult = await this.api('/voice/start', {
          method: 'POST',
          body: JSON.stringify({ brand: this.voiceBrand }),
        });
        this.toast('Voice call started', 'toast-success');
        await this.loadVoiceCalls();
      } catch (e) { this.toast('Voice call failed: ' + e.message, 'toast-error'); }
      finally { this.voiceLoading = false; }
    },
  };
}
