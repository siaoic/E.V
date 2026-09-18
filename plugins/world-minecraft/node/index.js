/**
 * Minecraft World 的 mineflayer 桥（由 Python 插件 world-minecraft 以子进程方式拉起）。
 *
 * 硬约束：stdout 只输出 NDJSON 协议帧（一行一个 JSON 对象），所有日志走 stderr。
 * 违反这条约束会污染协议流，Python 侧按协议违约直接报错，不做容错解析。
 *
 * 职责边界：本进程只报结构化 JSON，不做任何中文渲染（渲染全部在 Python 侧）；
 * 变更检测在本进程按 1 秒节拍比对关键字段哈希，只有真变了才推 state_changed。
 *
 * 与 Python 的协议（stdout 下行 / stdin 上行，均为一行 JSON）：
 *   下行：hello / state / state_changed / damage / death / chat /
 *         player_join / player_leave / task_done / error / exited
 *   上行：{ type: "command", id, verb, args }
 *
 * 配置经环境变量传入：MC_HOST / MC_PORT / MC_USERNAME / MC_VERSION /
 * MC_THREAT_RADIUS（敌对生物警戒半径，格）。
 */

'use strict';

const mineflayer = require('mineflayer');
const { pathfinder, Movements, GoalNear, GoalFollow } = require('mineflayer-pathfinder');

const HOST = process.env.MC_HOST || '127.0.0.1';
const PORT = Number(process.env.MC_PORT || '25565');
const USERNAME = process.env.MC_USERNAME || 'MaiBot';
const MC_VERSION = process.env.MC_VERSION || false;
const THREAT_RADIUS = Number(process.env.MC_THREAT_RADIUS || '8');
const STATE_TICK_MS = 1000;
const COMMAND_TIMEOUT_MS = 60000;

/** 向 Python 侧发一帧协议 JSON。 */
function send(frame) {
  process.stdout.write(JSON.stringify(frame) + '\n');
}

/** stderr 日志（永远不进 stdout）。 */
function log(message) {
  process.stderr.write(`[mc-node] ${message}\n`);
}

// 协议流保护：console.* 全部改道 stderr。mineflayer 内部会用 console.log 打印
// 原始错误栈（连接失败时 loader.js 的 console.log(err)），若不改道会污染协议流。
// 本文件所有协议帧都直接走 process.stdout.write，不受此处改道影响。
for (const method of ['log', 'info', 'warn', 'error', 'debug', 'trace']) {
  console[method] = (...args) => {
    const text = args
      .map((item) => (typeof item === 'string' ? item : require('util').inspect(item)))
      .join(' ');
    process.stderr.write(`[mc-node:console] ${text}\n`);
  };
}

process.on('uncaughtException', (err) => {
  send({ type: 'error', message: `未捕获异常: ${err && err.stack ? err.stack : err}` });
  process.exit(1);
});
process.on('uncaughtRejection', (err) => {
  send({ type: 'error', message: `未处理的 Promise 拒绝: ${err && err.stack ? err.stack : err}` });
});

// --------------------------------------------------------------------------- //
// 机器人创建与生命周期
// --------------------------------------------------------------------------- //

const bot = mineflayer.createBot({
  host: HOST,
  port: PORT,
  username: USERNAME,
  version: MC_VERSION || undefined,
  auth: 'offline',
});

bot.loadPlugin(pathfinder);

bot.once('spawn', () => {
  const movements = new Movements(bot);
  bot.pathfinder.setMovements(movements);
  send({ type: 'hello', host: HOST, port: PORT, username: bot.username });
  send({ type: 'state', state: collectState() });
  lastHash = JSON.stringify(stateSignature(collectState()));
  log(`已进入服务器 ${HOST}:${PORT} 作为 ${bot.username}`);
});

bot.on('kicked', (reason) => {
  send({ type: 'error', message: `被服务器踢出: ${reason}` });
});
bot.on('error', (err) => {
  send({ type: 'error', message: `连接错误: ${err && err.message ? err.message : err}` });
});
bot.on('end', (reason) => {
  send({ type: 'exited', reason: String(reason || '') });
  // EOF 就是 Python 侧的断线信号，进程自身直接退出，生命周期保持父子绑定
  process.exit(0);
});

bot.on('chat', (username, message) => {
  if (username === bot.username) return;
  send({ type: 'chat', player: username, text: String(message || '') });
});

bot.on('death', () => {
  deathSentAt = Date.now();
  send({ type: 'death' });
});

bot.on('playerJoined', (player) => {
  send({ type: 'player_join', player: player && player.username ? player.username : String(player) });
});
bot.on('playerLeft', (player) => {
  send({ type: 'player_leave', player: player && player.username ? player.username : String(player) });
});

// --------------------------------------------------------------------------- //
// 状态采集与变更检测（1 秒节拍）
// --------------------------------------------------------------------------- //

const HOSTILE_KEYWORDS = [
  'zombie', 'skeleton', 'creeper', 'spider', 'enderman', 'witch', 'pillager',
  'vindicator', 'husk', 'stray', 'drowned', 'phantom', 'slime', 'silverfish',
  'blaze', 'ghast', 'wither', 'dragon', 'vex', 'ravager', 'hoglin', 'piglin',
  'warden', 'breeze', 'bogged',
];

function entityKind(name) {
  const lower = String(name || '').toLowerCase();
  if (lower === 'player') return 'player';
  if (HOSTILE_KEYWORDS.some((keyword) => lower.includes(keyword))) return 'hostile';
  return 'passive';
}

function collectState() {
  const pos = bot.entity && bot.entity.position ? bot.entity.position : null;
  const entities = [];
  const entitySummary = new Map();
  let nearestThreat = null;

  for (const entity of Object.values(bot.entities || {})) {
    if (!entity || entity === bot.entity) continue;
    const name = entity.username || (entity.name ? entity.name.replace(/^minecraft:/, '') : 'unknown');
    const kind = entityKind(entity.username ? 'player' : name);
    const distance = bot.entity && entity.position
      ? bot.entity.position.distanceTo(entity.position)
      : Infinity;
    if (!Number.isFinite(distance)) continue;
    entities.push({ name, kind, distance: Math.round(distance * 10) / 10 });
    const key = `${kind}:${name}`;
    const entry = entitySummary.get(key) || { name, kind, count: 0, nearest: Infinity };
    entry.count += 1;
    entry.nearest = Math.min(entry.nearest, Math.round(distance * 10) / 10);
    entitySummary.set(key, entry);
    if (kind === 'hostile' && distance <= THREAT_RADIUS) {
      if (!nearestThreat || distance < nearestThreat.distance) {
        nearestThreat = { name, distance: Math.round(distance * 10) / 10 };
      }
    }
  }

  const inventory = [];
  for (const item of (bot.inventory ? bot.inventory.items() : [])) {
    const name = item ? item.name : null;
    if (!name) continue;
    const existing = inventory.find((entry) => entry.name === name);
    if (existing) existing.count += item.count;
    else inventory.push({ name, count: item.count });
  }
  inventory.sort((a, b) => b.count - a.count);

  const blockBelow = bot.blockAt(bot.entity.position.offset(0, -1, 0));

  return {
    position: pos
      ? { x: Math.round(pos.x * 10) / 10, y: Math.round(pos.y * 10) / 10, z: Math.round(pos.z * 10) / 10 }
      : null,
    health: bot.health != null ? Math.round(bot.health) : null,
    maxHealth: 20,
    food: bot.food != null ? Math.round(bot.food) : null,
    blockBelow: blockBelow ? blockBelow.name : null,
    hostileNearby: nearestThreat,
    hostileCount: [...entitySummary.values()]
      .filter((entry) => entry.kind === 'hostile')
      .reduce((sum, entry) => sum + entry.count, 0),
    entities: [...entitySummary.values()]
      .sort((a, b) => a.nearest - b.nearest)
      .slice(0, 12),
    playersOnline: Object.values(bot.players || {})
      .filter((player) => player && player.username)
      .map((player) => player.username),
    inventory: inventory.slice(0, 12),
    inventoryTotalCount: inventory.reduce((sum, entry) => sum + entry.count, 0),
  };
}

/** 只取参与变更比对的关键字段；position 按 0.5 格量化避免浮点抖动。 */
function stateSignature(state) {
  return {
    position: state.position
      ? {
          x: Math.round(state.position.x * 2) / 2,
          y: Math.round(state.position.y * 2) / 2,
          z: Math.round(state.position.z * 2) / 2,
        }
      : null,
    health: state.health,
    food: state.food,
    blockBelow: state.blockBelow,
    hostileNearby: state.hostileNearby,
    hostileCount: state.hostileCount,
    entities: state.entities,
    playersOnline: state.playersOnline,
    inventory: state.inventory,
    inventoryTotalCount: state.inventoryTotalCount,
  };
}

let lastHash = null;
let lastState = null;
let deathSentAt = 0;

setInterval(() => {
  if (!bot.entity) return;
  const state = collectState();
  const signature = JSON.stringify(stateSignature(state));
  if (signature === lastHash) return;

  const previous = lastState;
  lastHash = signature;
  lastState = state;

  // 掉血单独走 damage 帧（带伤害量），state_changed 不再重复报告血量下降
  const healthDropped = previous && state.health != null && previous.health != null && state.health < previous.health;
  if (healthDropped) {
    const cause = bot.recentDamage && bot.recentDamage.length
      ? bot.recentDamage[bot.recentDamage.length - 1]
      : null;
    send({
      type: 'damage',
      amount: previous.health - state.health,
      health: state.health,
      cause: cause ? String(cause) : '',
    });
  }

  const changed = previous ? changedFields(stateSignature(previous), stateSignature(state)) : ['all'];
  // 玩家进出已由 player_join / player_leave 帧单独上报，state_changed 里不再重复成事件
  const textFields = changed.filter((field) => field !== 'playersOnline');

  // 显著 = 出现敌对生物或敌对数量增加（掉血走 damage 帧、死亡走 death 帧，均不在此列）；
  // 现存敌对生物走动导致的距离变化只算一般变化，由 Python 侧按 debounce 上报
  const threatAppeared = state.hostileNearby != null && (!previous || !previous.hostileNearby);
  const threatGrew = previous != null && state.hostileCount > (previous.hostileCount || 0);
  const significant = threatAppeared || threatGrew;

  if (textFields.length === 0 && !significant) return;

  send({
    type: 'state_changed',
    state,
    changed: textFields,
    significant,
  });
}, STATE_TICK_MS);

/** 找出两份签名里发生变化的字段名（顶层）。 */
function changedFields(before, after) {
  const changed = [];
  for (const key of Object.keys(after)) {
    if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) changed.push(key);
  }
  return changed;
}

// --------------------------------------------------------------------------- //
// 命令执行（串行队列）
// --------------------------------------------------------------------------- //

const commandQueue = [];
let draining = false;

function enqueueCommand(frame) {
  commandQueue.push(frame);
  drainCommands();
}

async function drainCommands() {
  if (draining) return;
  draining = true;
  try {
    while (commandQueue.length > 0) {
      const frame = commandQueue.shift();
      const startedAt = Date.now();
      try {
        const result = await runCommand(frame);
        send({ type: 'task_done', id: frame.id, ok: true, detail: result.detail || '', data: result.data || null, elapsedMs: Date.now() - startedAt });
      } catch (err) {
        send({ type: 'task_done', id: frame.id, ok: false, detail: String(err && err.message ? err.message : err), data: null, elapsedMs: Date.now() - startedAt });
      }
    }
  } finally {
    draining = false;
  }
}

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error(`${label} 超时（${ms}ms）`)), ms)),
  ]);
}

async function runCommand(frame) {
  const verb = String(frame.verb || '').toLowerCase();
  const args = Array.isArray(frame.args) ? frame.args.map(String) : [];

  switch (verb) {
    case 'goto': {
      if (args[0] === 'player') {
        const target = args[1] ? Object.values(bot.players).find((player) => player.username === args[1]) : null;
        if (!target || !target.entity) throw new Error(`找不到玩家 ${args[1] || ''} 或其实体不在附近`);
        const goalTarget = target.entity.position;
        await withTimeout(
          bot.pathfinder.goto(new GoalNear(goalTarget.x, goalTarget.y, goalTarget.z, 2)),
          COMMAND_TIMEOUT_MS,
          `走向 ${args[1]}`,
        );
        return { detail: `已走到玩家 ${args[1]} 附近` };
      }
      const [x, y, z] = args.map(Number);
      if ([x, y, z].some((value) => !Number.isFinite(value))) throw new Error('goto 需要三个数字坐标');
      await withTimeout(bot.pathfinder.goto(new GoalNear(x, y, z, 1)), COMMAND_TIMEOUT_MS, `走到 (${x}, ${y}, ${z})`);
      return { detail: `已到达 (${x}, ${y}, ${z})` };
    }
    case 'follow': {
      const name = args[0] || '';
      const target = Object.values(bot.players).find((player) => player.username === name);
      if (!target || !target.entity) throw new Error(`找不到玩家 ${name} 或其实体不在附近`);
      bot.pathfinder.setGoal(new GoalFollow(target.entity, 3), true);
      return { detail: `开始跟随 ${name}（发送 stop 停止）` };
    }
    case 'stop': {
      bot.pathfinder.setGoal(null);
      bot.pathfinder.stop();
      return { detail: '已停止移动' };
    }
    case 'dig': {
      const blockName = args[0] || '';
      const blockType = bot.registry.blocksByName[blockName];
      if (!blockType) throw new Error(`未知方块类型 ${blockName}`);
      const found = bot.findBlock({ matching: blockType.id, maxDistance: 4 });
      if (!found) throw new Error(`4 格内没有找到 ${blockName}`);
      await withTimeout(bot.dig(found), COMMAND_TIMEOUT_MS, `挖掘 ${blockName}`);
      return { detail: `挖掉了 ${blockName}` };
    }
    case 'attack': {
      const wanted = (args[0] || '').toLowerCase();
      const candidates = Object.values(bot.entities).filter((entity) => {
        if (!entity || entity === bot.entity) return false;
        const name = entity.username || (entity.name ? entity.name.replace(/^minecraft:/, '') : '');
        if (wanted && !name.toLowerCase().includes(wanted)) return false;
        if (!wanted && entityKind(entity.username ? 'player' : name) !== 'hostile') return false;
        return bot.entity.position.distanceTo(entity.position) <= 4;
      });
      if (candidates.length === 0) throw new Error(wanted ? `4 格内没有找到 ${wanted}` : '4 格内没有敌对生物');
      candidates.sort((a, b) => bot.entity.position.distanceTo(a.position) - bot.entity.position.distanceTo(b.position));
      await bot.attack(candidates[0]);
      const name = candidates[0].username || candidates[0].name;
      return { detail: `攻击了 ${name}` };
    }
    case 'say': {
      const text = args.join(' ').trim();
      if (!text) throw new Error('say 需要内容');
      bot.chat(text.slice(0, 256));
      return { detail: `已在游戏内发言` };
    }
    case 'check': {
      return { detail: '已查询状态与背包', data: { state: collectState() } };
    }
    case 'scout': {
      return { detail: '已观察周围', data: { state: collectState(), scout: collectScout() } };
    }
    default:
      throw new Error(`未知动作 ${verb}（支持 goto/follow/stop/dig/attack/say/check/scout）`);
  }
}

function collectScout() {
  const blocks = new Map();
  const positions = bot.findBlocks({ matching: (block) => block != null, maxDistance: 6, count: 200 }) || [];
  for (const block of positions) {
    if (!block) continue;
    const key = block.name;
    blocks.set(key, (blocks.get(key) || 0) + 1);
  }
  return {
    blocksNearby: [...blocks.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 12)
      .map(([name, count]) => ({ name, count })),
  };
}

// --------------------------------------------------------------------------- //
// Python 上行命令
// --------------------------------------------------------------------------- //

let lineBuffer = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  lineBuffer += chunk;
  let newlineIndex = lineBuffer.indexOf('\n');
  while (newlineIndex !== -1) {
    const line = lineBuffer.slice(0, newlineIndex).trim();
    lineBuffer = lineBuffer.slice(newlineIndex + 1);
    if (line) handleUpstreamLine(line);
    newlineIndex = lineBuffer.indexOf('\n');
  }
});
process.stdin.on('end', () => {
  log('stdin 已关闭（Python 侧要求退出）');
  process.exit(0);
});

function handleUpstreamLine(line) {
  let frame;
  try {
    frame = JSON.parse(line);
  } catch (err) {
    // 上行帧损坏属于协议违约：直接暴露，不做容错解析
    send({ type: 'error', message: `无法解析上行协议帧: ${err && err.message ? err.message : err}` });
    return;
  }
  if (frame && frame.type === 'command') enqueueCommand(frame);
}

log(`mc-node 启动: ${HOST}:${PORT} username=${USERNAME} version=${MC_VERSION || '自动协商'}`);
