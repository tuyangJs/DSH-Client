// DSH Client 事件桥插件（组合预设版）
//
// 由 DSH-Client 自动注入到用户预设 dsh-client（复制自部署默认预设 standard + 本行）。
// 模块级单例：多个会话挂载同一预设时，路由与宿主级监听器只注册一份，事件环共享；
// 工具按会话注册（每个会话的 agent 各有一份），推送写入共享事件环。
// 注意：监听器随首个挂载会话的 fiber 生命周期；全部会话关闭后基础设施才卸载。
// 单会话使用（桌面客户端典型场景）下行为完全正常。

const MAX_EVENTS = 60

const shared = {
  refs: 0,
  infra: null,
  events: [],
  seq: 0,
  pendingAsks: new Map(),
  workflowPhases: new Map(),
}

function push(kind, fields) {
  const ev = { seq: ++shared.seq, ts: Date.now(), kind: kind }
  const obj = fields || {}
  for (const k of Object.keys(obj)) {
    const v = obj[k]
    if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') ev[k] = v
  }
  shared.events.push(ev)
  while (shared.events.length > MAX_EVENTS) shared.events.shift()
}

function registerTools(ctx) {
  const tools = ctx.get('tools')
  if (tools === undefined) return []

  const notifyTool = {
    name: 'desktop_notify',
    description: '向桌面客户端发送一条 Windows 原生通知（DSH Client 事件桥）。适合长任务完成、需要用户注意时使用。title 必填，body 可选。',
    timeoutMs: 30000,
    parameters: {
      type: 'object',
      properties: {
        title: { type: 'string', description: '通知标题' },
        body: { type: 'string', description: '通知正文（可选）' },
      },
      required: ['title'],
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: { ok: { type: 'boolean' } },
      },
      render: (_args, value) => [{ type: 'text', text: value.ok ? '已发送桌面通知' : '桌面通知发送失败' }],
    },
    async execute(args) {
      push('desktop-notify', {
        title: String(args.title || ''),
        body: String(args.body || ''),
      })
      return { ok: true }
    },
  }

  const askTool = {
    name: 'desktop_ask',
    description: '通过桌面客户端弹出一个带两个按钮的 Windows 原生通知，并阻塞等待用户点击。返回用户选择：ok / cancel / timeout（15 分钟无应答）/ cancelled（调用被中止）。适合需要用户决策的交互式流程。',
    timeoutMs: 15 * 60 * 1000,
    parameters: {
      type: 'object',
      properties: {
        title: { type: 'string', description: '问题标题' },
        body: { type: 'string', description: '问题说明（可选）' },
        button1: { type: 'string', description: '第一个按钮文本（默认「确认」）' },
        button2: { type: 'string', description: '第二个按钮文本（默认「取消」，可空）' },
      },
      required: ['title'],
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: { choice: { type: 'string' } },
      },
      render: (_args, value) => [{ type: 'text', text: '用户选择: ' + String(value.choice) }],
    },
    async execute(args, exec) {
      const requestId = 'ask-' + Date.now() + '-' + Math.floor(Math.random() * 100000)
      push('desktop-ask', {
        requestId: requestId,
        title: String(args.title || ''),
        body: String(args.body || ''),
        button1: String(args.button1 || '确认'),
        button2: String(args.button2 || '取消'),
      })
      return new Promise((resolve) => {
        let settled = false
        let cancelTimer = null
        const finish = (choice) => {
          if (settled) return
          settled = true
          if (cancelTimer) { try { cancelTimer() } catch (e) {} cancelTimer = null }
          shared.pendingAsks.delete(requestId)
          resolve({ choice: choice })
        }
        shared.pendingAsks.set(requestId, { finish: finish })
        cancelTimer = ctx.timeout(() => finish('timeout'), 15 * 60 * 1000)
        if (exec.signal) {
          exec.signal.addEventListener('abort', () => finish('cancelled'), { once: true })
        }
      })
    },
  }

  return [tools.register(notifyTool), tools.register(askTool)]
}

function registerInfra(ctx) {
  const webServer = ctx.get('webServer')
  const disposers = []
  if (webServer !== undefined) {
    disposers.push(ctx.on('agent/status', (payload) => {
      try {
        const agent = payload && payload.agent
        const status = payload && payload.status
        if (!agent || typeof agent.id !== 'string') return
        if (status === 'running') push('agent-running', { sessionId: agent.id })
        else if (status === 'idle') push('agent-idle', { sessionId: agent.id })
      } catch (e) {
        console.error('[client-bridge] agent/status:', e)
      }
    }))

    disposers.push(ctx.on('agent/error', (payload) => {
      try {
        const agent = payload && payload.agent
        if (!agent || typeof agent.id !== 'string') return
        let message = ''
        const err = payload.error
        if (err && typeof err === 'object' && typeof err.message === 'string') message = err.message
        else if (typeof err === 'string') message = err
        push('agent-error', { sessionId: agent.id, message: message.slice(0, 240) })
      } catch (e) {
        console.error('[client-bridge] agent/error:', e)
      }
    }))

    disposers.push(ctx.on('subagent/end', (info) => {
      try {
        if (!info || typeof info.id !== 'string') return
        push('subagent-end', {
          sessionId: info.id,
          provider: typeof info.provider === 'string' ? info.provider : '',
          stopReason: typeof info.stopReason === 'string' ? info.stopReason : '',
        })
      } catch (e) {
        console.error('[client-bridge] subagent/end:', e)
      }
    }))

    disposers.push(ctx.on('agent/pre-step', (payload, next) => {
      try {
        const agent = payload && payload.agent
        const step = payload && payload.step
        if (agent && typeof agent.id === 'string' && typeof step === 'number') {
          push('progress', {
            sessionId: agent.id,
            value: Math.min(90, step * 5),
            label: 'step ' + step,
          })
        }
      } catch (e) {
        console.error('[client-bridge] agent/pre-step:', e)
      }
      return next()
    }))

    disposers.push(ctx.on('workflow/start', (info) => {
      try {
        const phases = info && info.meta && Array.isArray(info.meta.phases) ? info.meta.phases : []
        shared.workflowPhases.set(String(info.id), phases)
        push('progress', {
          sessionId: String(info.id),
          value: 0,
          label: (info.meta && typeof info.meta.name === 'string') ? info.meta.name : 'workflow',
        })
      } catch (e) {
        console.error('[client-bridge] workflow/start:', e)
      }
    }))

    disposers.push(ctx.on('workflow/phase', (info, title) => {
      try {
        const phases = shared.workflowPhases.get(String(info.id)) || []
        const total = phases.length
        if (total > 0) {
          const idx = phases.findIndex((p) => p && p.title === title)
          const i = idx >= 0 ? idx + 1 : 1
          push('progress', {
            sessionId: String(info.id),
            value: Math.min(95, Math.round(i * 100 / total)),
            label: String(title),
          })
        }
      } catch (e) {
        console.error('[client-bridge] workflow/phase:', e)
      }
    }))

    disposers.push(ctx.on('workflow/end', (info) => {
      try {
        shared.workflowPhases.delete(String(info.id))
        push('progress', { sessionId: String(info.id), value: 100, label: 'done' })
      } catch (e) {
        console.error('[client-bridge] workflow/end:', e)
      }
    }))

    disposers.push(webServer.register({
      kind: 'exact',
      path: '/client-bridge/events',
      handler: (req, res) => {
        try {
          let since = 0
          const m = /[?&]since=(\d+)/.exec(req.url || '')
          if (m) since = parseInt(m[1], 10) || 0
          const fresh = shared.events.filter((ev) => ev.seq > since)
          res.writeHead(200, {
            'Content-Type': 'application/json; charset=utf-8',
            'Cache-Control': 'no-store',
            'Access-Control-Allow-Origin': '*',
          })
          res.end(JSON.stringify({ seq: shared.seq, events: fresh }))
        } catch (e) {
          try {
            res.writeHead(500, { 'Content-Type': 'application/json; charset=utf-8' })
            res.end(JSON.stringify({ error: String((e && e.message) || e) }))
          } catch (e2) { /* ignore */ }
        }
      },
    }))

    disposers.push(webServer.register({
      kind: 'exact',
      path: '/client-bridge/respond',
      handler: (req, res) => {
        const done = (code, payload) => {
          try {
            res.writeHead(code, {
              'Content-Type': 'application/json; charset=utf-8',
              'Access-Control-Allow-Origin': '*',
            })
            res.end(JSON.stringify(payload))
          } catch (e) { /* ignore */ }
        }
        try {
          if (req.method !== 'POST') {
            done(405, { ok: false, error: 'POST only' })
            return
          }
          let raw = ''
          req.setEncoding('utf8')
          req.on('data', (c) => {
            raw += c
            if (raw.length > 64 * 1024) raw = raw.slice(0, 64 * 1024)
          })
          req.on('end', () => {
            let ok = false
            try {
              const body = JSON.parse(raw || '{}')
              const rid = String(body.requestId || '')
              const choice = String(body.choice || '')
              const entry = shared.pendingAsks.get(rid)
              if (entry && (choice === 'ok' || choice === 'cancel')) {
                entry.finish(choice)
                ok = true
              }
            } catch (e) {
              console.error('[client-bridge] respond parse:', e)
            }
            done(ok ? 200 : 400, { ok: ok })
          })
        } catch (e) {
          done(500, { ok: false, error: String((e && e.message) || e) })
        }
      },
    }))
  }
  return disposers
}

export default {
  inject: ['timer'],
  apply(ctx) {
    shared.refs += 1
    const ownDisposers = registerTools(ctx)
    if (shared.infra === null) {
      shared.infra = { disposers: registerInfra(ctx) }
    }
    ctx.effect(() => () => {
      for (const d of ownDisposers) {
        try { d() } catch (e) { /* ignore */ }
      }
      shared.refs -= 1
      if (shared.refs <= 0 && shared.infra !== null) {
        for (const d of shared.infra.disposers) {
          try { d() } catch (e) { /* ignore */ }
        }
        shared.infra = null
        for (const [, entry] of shared.pendingAsks) {
          try { entry.finish('cancelled') } catch (e) { /* ignore */ }
        }
        shared.pendingAsks.clear()
      }
    })
  },
}
