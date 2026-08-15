import { readFileSync, writeFileSync, existsSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

const pluginConfigPath = join(__dirname, '..', '..', '..', 'live-2d', 'plugins', 'built-in', 'minecraft', 'plugin_config.json');
const keysPath = join(__dirname, 'keys.json');
const andyPath = join(__dirname, 'andy.json');

function getVal(cfg, key) {
    const field = cfg[key];
    if (field && typeof field === 'object' && 'value' in field) return field.value;
    return field || '';
}

if (!existsSync(pluginConfigPath)) {
    console.log('未找到 plugin_config.json，跳过同步');
    process.exit(0);
}

let cfg;
try {
    cfg = JSON.parse(readFileSync(pluginConfigPath, 'utf8'));
} catch (e) {
    console.log('读取 plugin_config.json 失败:', e.message);
    process.exit(0);
}

// 同步 keys.json
const apiKey = getVal(cfg, 'api_key');
if (apiKey) {
    let keys = {};
    if (existsSync(keysPath)) {
        try { keys = JSON.parse(readFileSync(keysPath, 'utf8')); } catch (e) {}
    }
    keys.OPENAI_API_KEY = apiKey;
    writeFileSync(keysPath, JSON.stringify(keys, null, 4), 'utf8');
    console.log('已同步 API KEY 到 keys.json');
}

// 同步 andy.json
if (existsSync(andyPath)) {
    try {
        const andy = JSON.parse(readFileSync(andyPath, 'utf8'));
        const agentName = getVal(cfg, 'agent_name');
        const modelName = getVal(cfg, 'model_name');
        const modelUrl  = getVal(cfg, 'model_url');
        const conversing = getVal(cfg, 'conversing');
        if (agentName) andy.name = agentName;
        if (modelName) { andy.model = andy.model || {}; andy.model.model = modelName; }
        if (modelUrl)  { andy.model = andy.model || {}; andy.model.url = modelUrl; }
        if (conversing) andy.conversing = conversing;
        writeFileSync(andyPath, JSON.stringify(andy, null, 4), 'utf8');
        console.log('已同步配置到 andy.json');
    } catch (e) {
        console.log('同步 andy.json 失败:', e.message);
    }
}
