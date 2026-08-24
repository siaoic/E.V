import { spawn } from 'child_process';
import { logoutAgent } from '../mindcraft/mindserver.js';
import { fileURLToPath } from 'url';
import path from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

export class AgentProcess {
    constructor(name, port) {
        this.name = name;
        this.port = port;
    }

    start(load_memory=false, init_message=null, count_id=0) {
        this.count_id = count_id;
        this.running = true;

        let args = ['src/process/init_agent.js', this.name];
        args.push('-n', this.name);
        args.push('-c', count_id);
        if (load_memory)
            args.push('-l', load_memory);
        if (init_message)
            args.push('-m', init_message);
        args.push('-p', this.port);

        // 尝试多种 Node.js 路径
        const possibleNodePaths = [
            'node', // 系统 Node.js
            './node/node.exe', // 便携版
            path.resolve(__dirname, '../../node/node.exe'), // 绝对路径
        ];

        let nodePath = possibleNodePaths[0]; // 默认使用系统 node
        
        // 如果检测到便携版存在，则使用便携版
        try {
            const portableNodePath = path.resolve(__dirname, '../../node/node.exe');
            require('fs').accessSync(portableNodePath, require('fs').constants.F_OK);
            nodePath = portableNodePath;
            console.log('Using portable Node.js:', nodePath);
        } catch (e) {
            console.log('Portable Node.js not found, using system Node.js');
        }

        const agentProcess = spawn(nodePath, args, {
            stdio: 'inherit',
            stderr: 'inherit',
            shell: process.platform === 'win32', // Windows 需要 shell
        });
        
        let last_restart = Date.now();
        agentProcess.on('exit', (code, signal) => {
            console.log(`Agent process exited with code ${code} and signal ${signal}`);
            this.running = false;
            logoutAgent(this.name);
            
            if (code > 1) {
                console.log(`Ending task`);
                process.exit(code);
            }

            if (code !== 0 && signal !== 'SIGINT') {
                // agent must run for at least 10 seconds before restarting
                if (Date.now() - last_restart < 10000) {
                    console.error(`Agent process exited too quickly and will not be restarted.`);
                    return;
                }
                console.log('Restarting agent...');
                this.start(true, 'Agent process restarted.', count_id, this.port);
                last_restart = Date.now();
            }
        });
    
        agentProcess.on('error', (err) => {
            console.error('Agent process error:', err);
        });

        this.process = agentProcess;
    }

    stop() {
        if (!this.running) return;
        this.process.kill('SIGINT');
    }

    continue() {
        if (!this.running) {
            this.start(true, 'Agent process restarted.', this.count_id);
        }
    }
}