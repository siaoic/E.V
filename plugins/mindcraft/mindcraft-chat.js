// mindcraft-chat.js (ES模块版本)
import { io } from 'socket.io-client';
import readline from 'readline';

// 连接服务器
const socket = io('http://localhost:8080');

// 连接成功
socket.on('connect', () => {
    console.log('已连接到Mindcraft服务器');
    socket.emit('listen-to-agents');
    console.log('现在可以输入消息与机器人聊天了！');
    rl.prompt();
});

// 接收机器人回复
socket.on('bot-output', (agentName, message) => {
    console.log(`\n[${agentName}] 回复: ${message}`);
    rl.prompt();
});

// 连接错误
socket.on('connect_error', (error) => {
    console.log('连接失败:', error.message);
});

// 发送消息函数
function sendMessage(agentName, message) {
    socket.emit('send-message', agentName, { 
        from: 'ADMIN', 
        message: message 
    });
    console.log(`你: ${message}`);
}

// 命令行交互
const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    prompt: '输入消息 > '
});

console.log('正在连接到Mindcraft服务器...');
console.log('命令: quit=退出, !stop=停止动作, !stay=保持静止');

rl.on('line', (input) => {
    const message = input.trim();
    if (message.toLowerCase() === 'quit' || message.toLowerCase() === 'q') {
        console.log('正在断开连接...');
        socket.disconnect();
        rl.close();
        process.exit(0);
    } else if (message) {
        sendMessage('fake-neuro', message);
    }
    rl.prompt();
});

// 优雅退出
process.on('SIGINT', () => {
    console.log('\n正在断开连接...');
    socket.disconnect();
    rl.close();
    process.exit(0);
});