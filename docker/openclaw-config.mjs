import fs from 'node:fs';

const stateDir = process.env.OPENCLAW_STATE_DIR || '/home/node/.openclaw';
const model = process.env.AGENT_MODEL;
const temperature = Number(process.env.AGENT_TEMPERATURE ?? '0');
const contextWindow = Number(process.env.AGENT_CONTEXT_WINDOW ?? '128000');
const runName = process.env.BENCH_RUN_NAME || 'openclaw-demo';
if (!model || !process.env.OPENCLAW_GATEWAY_TOKEN) throw new Error('AGENT_MODEL and OPENCLAW_GATEWAY_TOKEN are required');
if (!/^[A-Za-z0-9_-]+$/.test(runName)) throw new Error('BENCH_RUN_NAME must be a simple directory name');
const workspace = `${stateDir}/workspace/${runName}`;
fs.mkdirSync(workspace, { recursive: true });
const config = {
  gateway: {
    mode: 'local', bind: 'lan', port: 18789,
    auth: { mode: 'token', token: process.env.OPENCLAW_GATEWAY_TOKEN },
    controlUi: { enabled: false },
    http: { endpoints: { chatCompletions: { enabled: true } } }
  },
  agents: { defaults: { workspace, model: { primary: `bench/${model}` }, models: { [`bench/${model}`]: { params: { temperature } } }, maxConcurrent: 1 } },
  models: { mode: 'replace', providers: { bench: {
    baseUrl: 'http://model-proxy:8080/v1', apiKey: 'proxy-managed', api: 'openai-completions',
    models: [{ id: model, name: model, reasoning: false, input: ['text'], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow, maxTokens: 4096 }]
  } } },
  mcp: { servers: { world: { url: 'http://world:8000/mcp', transport: 'streamable-http', enabled: true, toolFilter: { include: ['world_get', 'world_set', 'notify_user'] } } } },
  tools: { profile: 'minimal', alsoAllow: ['world__world_get', 'world__world_set', 'world__notify_user'], deny: ['group:fs', 'group:runtime', 'group:web', 'group:sessions', 'gateway', 'cron', 'browser', 'nodes', 'message'] },
  channels: {},
  plugins: { entries: {} }
};
fs.writeFileSync(`${stateDir}/openclaw.json`, JSON.stringify(config, null, 2), { mode: 0o600 });
